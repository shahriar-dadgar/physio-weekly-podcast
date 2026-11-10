#!/usr/bin/env python3
"""Physio Weekly for Sabaa: podcast builder.

1. Fetches the latest English podcast script from the PhysioCollector
   Apps Script web app (secret token).
2. Skips if that week's episode is already published (safe to re-run).
3. Turns it into an MP3 with Edge TTS (no API key, no card).
4. Keeps the last episodes and rebuilds docs/feed.xml + docs/index.html.

Required environment variables (GitHub Actions secrets):
  APPS_SCRIPT_URL   your web app URL (ends in /exec)
  PODCAST_TOKEN     the token created by setPodcastToken() in Apps Script

Optional:
  TTS_VOICE         default en-US-EmmaMultilingualNeural
  TTS_RATE          default +0%   (e.g. -5% for slower speech)
  KEEP_EPISODES     default 26
  SHOW_TITLE        default "Physio Weekly for Sabaa"
  BASE_URL          override the public site URL
  FORCE             set to 1 to rebuild an episode that already exists
"""

import asyncio
import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path("docs")
EPISODES = DOCS / "episodes"

KEEP_EPISODES = int(os.environ.get("KEEP_EPISODES", "26"))
VOICE = os.environ.get("TTS_VOICE", "en-US-EmmaMultilingualNeural")
RATE = os.environ.get("TTS_RATE", "+0%")
SHOW_TITLE = os.environ.get("SHOW_TITLE", "Physio Weekly for Sabaa")
SHOW_DESCRIPTION = (
    "A short weekly briefing on the latest physiotherapy and rehabilitation "
    "research and technology."
)
FORCE = os.environ.get("FORCE", "").strip() in ("1", "true", "yes")


# ------------------------------------------------------------
# 1. FETCH THE SCRIPT
# ------------------------------------------------------------

def fetch_script():
    base = os.environ["APPS_SCRIPT_URL"].strip()
    token = os.environ["PODCAST_TOKEN"].strip()

    query = urllib.parse.urlencode({"action": "podcast", "token": token})
    url = base + ("&" if "?" in base else "?") + query

    last_error = "unknown error"

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))

            if data.get("ok") and data.get("script"):
                return data["date"], data["script"]

            last_error = data.get("error", "unexpected response")

            if last_error == "No podcast script yet":
                print("No podcast script in the sheet yet. Nothing to do.")
                sys.exit(0)

        except Exception as err:  # never print the URL: it holds the token
            last_error = type(err).__name__ + ": " + str(err)[:150]

        print("Fetch attempt %d failed: %s" % (attempt, last_error), file=sys.stderr)
        time.sleep(30 * attempt)

    sys.exit("Could not fetch the podcast script: " + last_error)


def clean_for_speech(text):
    """Second safety net: the Apps Script already removes markdown."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`~#|<>\\{}]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------
# 2. TEXT TO SPEECH
# ------------------------------------------------------------

async def _tts(text, out_path):
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(out_path))


def synthesize(text, out_path):
    last_error = "unknown error"

    for attempt in range(1, 4):
        try:
            asyncio.run(_tts(text, out_path))

            if out_path.exists() and out_path.stat().st_size > 10_000:
                return

            last_error = "audio file too small"

        except Exception as err:
            last_error = type(err).__name__ + ": " + str(err)[:150]

        print("TTS attempt %d failed: %s" % (attempt, last_error), file=sys.stderr)
        time.sleep(10 * attempt)

    sys.exit("Text-to-speech failed: " + last_error)


def duration_seconds(path, text):
    try:
        from mutagen.mp3 import MP3

        return int(MP3(str(path)).info.length)
    except Exception:
        return int(len(text.split()) / 150 * 60)  # rough estimate


# ------------------------------------------------------------
# 3. EPISODES
# ------------------------------------------------------------

def save_episode(date_text, script):
    EPISODES.mkdir(parents=True, exist_ok=True)

    mp3_path = EPISODES / (date_text + ".mp3")

    synthesize(script, mp3_path)

    day = dt.date.fromisoformat(date_text)

    pretty = "%s %d, %d" % (day.strftime("%B"), day.day, day.year)

    description = " ".join(script.split())[:400]

    meta = {
        "date": date_text,
        "title": "%s: %s" % (SHOW_TITLE, pretty),
        "description": description,
        "duration": duration_seconds(mp3_path, script),
        "bytes": mp3_path.stat().st_size,
    }

    (EPISODES / (date_text + ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def prune():
    dates = sorted((p.stem for p in EPISODES.glob("*.json")), reverse=True)

    for old in dates[KEEP_EPISODES:]:
        for ext in (".json", ".mp3"):
            (EPISODES / (old + ext)).unlink(missing_ok=True)


# ------------------------------------------------------------
# 4. COVER ART (drawn once, if docs/cover.jpg is missing)
# ------------------------------------------------------------

def _font(size, bold=False):
    from PIL import ImageFont

    names = (
        ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
        if bold
        else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    )

    for folder in ("/usr/share/fonts/truetype/dejavu",
                   "/usr/share/fonts/truetype/liberation", ""):
        for name in names:
            try:
                return ImageFont.truetype(os.path.join(folder, name), size)
            except OSError:
                pass

    return ImageFont.load_default(size=size)


def make_cover(path):
    from PIL import Image, ImageDraw

    size = 1400
    img = Image.new("RGB", (size, size), (14, 61, 66))
    draw = ImageDraw.Draw(img)

    # soft diagonal light
    for y in range(size):
        shade = int(22 * y / size)
        draw.line([(0, y), (size, y)], fill=(14 + shade, 61 + shade, 66 + shade))

    # sound-wave bars that rise like a recovery curve
    bars = 23
    left, right, base_y = 170, size - 170, 900
    step = (right - left) / (bars - 1)
    for i in range(bars):
        x = left + i * step
        t = i / (bars - 1)
        height = 40 + 300 * (t ** 1.6) * (0.75 + 0.25 * ((i % 3) / 2))
        draw.rounded_rectangle(
            [x - 14, base_y - height, x + 14, base_y],
            radius=14,
            fill=(120, 214, 196),
        )

    draw.line([(170, base_y + 40), (size - 170, base_y + 40)],
              fill=(120, 214, 196), width=6)

    draw.text((170, 230), "PHYSIO", font=_font(170, True), fill=(245, 248, 246))
    draw.text((170, 410), "WEEKLY", font=_font(170, True), fill=(120, 214, 196))
    draw.text((170, 1010), "for Sabaa", font=_font(96), fill=(245, 248, 246))
    draw.text((170, 1150), "Research & rehab technology, every Monday",
              font=_font(44), fill=(190, 222, 215))

    img.save(path, "JPEG", quality=90)


# ------------------------------------------------------------
# 5. FEED + INDEX PAGE
# ------------------------------------------------------------

def base_url():
    override = os.environ.get("BASE_URL", "").strip()

    if override:
        return override.rstrip("/")

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")

    return "https://%s.github.io/%s" % (owner.lower(), repo)


def load_episodes():
    metas = []

    for path in sorted(EPISODES.glob("*.json"), reverse=True):
        metas.append(json.loads(path.read_text(encoding="utf-8")))

    return metas


def build_feed(base):
    items = []

    for meta in load_episodes():
        date_text = meta["date"]

        published = email.utils.format_datetime(
            dt.datetime.fromisoformat(date_text).replace(
                hour=6, tzinfo=dt.timezone.utc
            )
        )

        title = escape(meta["title"])
        description = escape(meta["description"])
        audio_url = "%s/episodes/%s.mp3" % (base, date_text)

        items.append(
            "    <item>\n"
            "      <title>%s</title>\n"
            "      <description>%s</description>\n"
            "      <pubDate>%s</pubDate>\n"
            '      <guid isPermaLink="false">physio-weekly-%s</guid>\n'
            '      <enclosure url="%s" length="%d" type="audio/mpeg"/>\n'
            "      <itunes:duration>%d</itunes:duration>\n"
            "    </item>"
            % (title, description, published, date_text, audio_url,
               meta["bytes"], meta["duration"])
        )

    show = escape(SHOW_TITLE)

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        "  <channel>\n"
        "    <title>%s</title>\n"
        "    <link>%s/</link>\n"
        "    <description>%s</description>\n"
        "    <language>en</language>\n"
        "    <itunes:author>%s</itunes:author>\n"
        '    <itunes:category text="Health &amp; Fitness"><itunes:category text="Medicine"/></itunes:category>\n'
        "    <itunes:explicit>false</itunes:explicit>\n"
        '    <itunes:image href="%s/cover.jpg"/>\n'
        "    <image><url>%s/cover.jpg</url><title>%s</title><link>%s/</link></image>\n"
        "%s\n"
        "  </channel>\n"
        "</rss>\n"
    ) % (show, base, escape(SHOW_DESCRIPTION), show, base, base, show, base,
         "\n".join(items))

    (DOCS / "feed.xml").write_text(feed, encoding="utf-8")


def build_index(base):
    rows = []

    for meta in load_episodes():
        rows.append(
            "<li><strong>%s</strong><br>"
            '<audio controls preload="none" src="episodes/%s.mp3"></audio></li>'
            % (escape(meta["title"]), meta["date"])
        )

    page = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"icon\" href=\"cover.jpg\">"
        "<title>%s</title></head><body style=\"font-family:sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem\">"
        "<img src=\"cover.jpg\" alt=\"\" style=\"width:160px;border-radius:12px\">"
        "<h1>%s</h1><p>%s</p><p>Podcast feed: <code>%s/feed.xml</code></p><ul>%s</ul></body></html>"
    ) % (escape(SHOW_TITLE), escape(SHOW_TITLE), escape(SHOW_DESCRIPTION),
         base, "".join(rows))

    (DOCS / "index.html").write_text(page, encoding="utf-8")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    for name in ("APPS_SCRIPT_URL", "PODCAST_TOKEN"):
        if not os.environ.get(name, "").strip():
            sys.exit("Missing secret: " + name)

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()

    if not (DOCS / "cover.jpg").exists():
        make_cover(DOCS / "cover.jpg")

    date_text, script = fetch_script()

    if (EPISODES / (date_text + ".json")).exists() and not FORCE:
        print("Episode %s is already published. Nothing to do." % date_text)
        return

    save_episode(date_text, clean_for_speech(script))

    prune()

    base = base_url()

    build_feed(base)
    build_index(base)

    print("Episode %s ready. Feed: %s/feed.xml" % (date_text, base))


if __name__ == "__main__":
    main()

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

def fetch_episode(date_text=None):
    """Latest episode (or the one for date_text) from the Apps Script web app.

    Returns the JSON dict: date, script, stories. For a specific date,
    returns None instead of stopping when it can't be fetched.
    """
    base = os.environ["APPS_SCRIPT_URL"].strip()
    token = os.environ["PODCAST_TOKEN"].strip()

    params = {"action": "podcast", "token": token}
    if date_text:
        params["date"] = date_text

    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode(params)

    last_error = "unknown error"

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))

            if data.get("ok") and data.get("script"):
                return data

            last_error = data.get("error", "unexpected response")

            if last_error == "No podcast script yet":
                if date_text:
                    return None
                print("No podcast script in the sheet yet. Nothing to do.")
                sys.exit(0)

        except Exception as err:  # never print the URL: it holds the token
            last_error = type(err).__name__ + ": " + str(err)[:150]

        print("Fetch attempt %d failed: %s" % (attempt, last_error), file=sys.stderr)
        time.sleep(30 * attempt)

    if date_text:
        print("Could not fetch %s: %s" % (date_text, last_error), file=sys.stderr)
        return None

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
        "summary": sentence_trim(script, 700),
        "duration": duration_seconds(mp3_path, script),
        "bytes": mp3_path.stat().st_size,
    }

    (EPISODES / (date_text + ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def prune():
    dates = sorted((p.stem for p in EPISODES.glob("*.json")), reverse=True)

    for old in dates[KEEP_EPISODES:]:
        for ext in (".json", ".mp3", ".html"):
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


def use_custom_cover(src, dest):
    """Turns your own artwork (docs/cover.png) into a 1400x1400 cover.jpg."""
    from PIL import Image

    img = Image.open(src).convert("RGB")
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((1400, 1400), Image.LANCZOS)
    img.save(dest, "JPEG", quality=88, optimize=True, progressive=True)


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


def sentence_trim(text, limit):
    """Shortens text to whole sentences, at most `limit` characters."""
    text = " ".join(str(text or "").split())

    if len(text) <= limit and text[-1:] in (".", "?", "!"):
        return text

    cut = text[:limit] + " "
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))

    return cut[: stop + 1] if stop > len(cut) * 0.4 else cut.rsplit(" ", 1)[0] + "\u2026"


def pretty_minutes(seconds):
    minutes = max(1, round(int(seconds or 0) / 60))
    return "%d min" % minutes


def pretty_date(date_text):
    day = dt.date.fromisoformat(date_text)
    return "%s %d %s %d" % (day.strftime("%A,"), day.day, day.strftime("%B"), day.year)


def attr(text):
    return escape(str(text or ""), {'"': "&quot;"})


def clean_stories(stories):
    out = {"scientific": [], "technology": []}

    for kind in out:
        for story in (stories or {}).get(kind) or []:
            title = " ".join(str(story.get("title") or "").split())
            if title:
                out[kind].append({
                    "title": title,
                    "source": " ".join(str(story.get("source") or "").split()),
                    "url": str(story.get("url") or "").strip(),
                })

    return out


def transcript_paragraphs(script):
    text = str(script or "").replace("\r", "")
    parts = [p for p in re.split(r"\n\s*\n", text) if p.strip()]

    if len(parts) <= 1:
        parts = [p for p in text.split("\n") if p.strip()]

    return ["<p>%s</p>" % escape(" ".join(p.split())) for p in parts]


def write_extras(date_text, data):
    """Saves the story titles and a full transcript page for one episode."""
    meta_path = EPISODES / (date_text + ".json")

    if not meta_path.exists():
        return

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    stories = clean_stories(data.get("stories"))
    if stories["scientific"] or stories["technology"]:
        meta["stories"] = stories

    script = clean_for_speech(data.get("script") or "")

    if script:
        page = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<link rel=\"icon\" href=\"../cover.jpg\">"
            "<title>Transcript: %s</title><style>%s</style></head><body><div class=\"wrap\">"
            "<p><a class=\"back\" href=\"../\">&larr; All episodes</a></p>"
            "<span class=\"label\">Transcript</span>"
            "<h1>%s</h1><p class=\"meta\">%s &middot; %s</p>"
            "<audio controls preload=\"metadata\" src=\"%s.mp3\"></audio>"
            "<article class=\"transcript\">%s</article>"
            "<p><a class=\"back\" href=\"../\">&larr; All episodes</a></p>"
            "</div></body></html>"
        ) % (
            escape(meta["title"]), PAGE_STYLE, escape(meta["title"]),
            pretty_date(date_text), pretty_minutes(meta.get("duration")),
            date_text, "".join(transcript_paragraphs(script)),
        )

        (EPISODES / (date_text + ".html")).write_text(page, encoding="utf-8")
        meta["transcript"] = True

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def stories_html(meta):
    stories = meta.get("stories") or {}
    blocks = []

    for kind, heading in (("scientific", "Research"), ("technology", "Technology")):
        items = []

        for story in stories.get(kind) or []:
            title = escape(story["title"])
            if story.get("url"):
                title = '<a href="%s" target="_blank" rel="noopener">%s</a>' % (attr(story["url"]), title)
            source = ('<span class="src">%s</span>' % escape(story["source"])) if story.get("source") else ""
            items.append("<li>%s%s</li>" % (title, source))

        if items:
            blocks.append("<h4>%s</h4><ol>%s</ol>" % (heading, "".join(items)))

    return '<div class="stories">%s</div>' % "".join(blocks) if blocks else ""


PAGE_STYLE = """
:root{--bg:#f4f8f7;--card:#ffffff;--ink:#12302f;--muted:#55706d;--line:#d9e6e3;
--brand:#0e3d42;--accent:#2f9e8a;--accent-soft:#e2f4f0}
@media (prefers-color-scheme:dark){:root{--bg:#0b1d1f;--card:#122a2d;--ink:#e8f3f1;
--muted:#9db8b4;--line:#22403f;--brand:#0e3d42;--accent:#78d6c4;--accent-soft:#163a3a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:44rem;margin:0 auto;padding:2rem 1rem 3rem}
header{display:flex;gap:1.25rem;align-items:center;margin-bottom:1.75rem}
header img{width:96px;height:96px;border-radius:16px;flex:none}
h1{font-size:1.6rem;line-height:1.2;margin:0 0 .25rem}
header p{margin:0;color:var(--muted)}
.label{display:inline-block;font-size:.75rem;font-weight:700;letter-spacing:.08em;
text-transform:uppercase;color:var(--accent);background:var(--accent-soft);
padding:.2rem .6rem;border-radius:999px;margin-bottom:.75rem}
.latest{background:var(--card);border:1px solid var(--line);border-radius:18px;
padding:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.latest h2{font-size:1.35rem;line-height:1.3;margin:0 0 .25rem}
.meta{color:var(--muted);font-size:.9rem;margin:0 0 1rem}
audio{width:100%;margin:.25rem 0 1rem}
.summary{margin:0}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:1.25rem}
.btn{display:inline-block;font:inherit;font-size:.9rem;padding:.5rem .9rem;border-radius:10px;
border:1px solid var(--line);background:transparent;color:var(--ink);text-decoration:none;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#06211f;font-weight:600}
h3{font-size:1rem;margin:2.25rem 0 .75rem;color:var(--muted);font-weight:600}
ul{list-style:none;margin:0;padding:0}
li{border-top:1px solid var(--line);padding:.9rem 0}
li strong{display:block}
li span{color:var(--muted);font-size:.85rem}
li audio{margin:.5rem 0 0;height:36px}
.empty{color:var(--muted)}
footer{margin-top:2.5rem;color:var(--muted);font-size:.85rem;word-break:break-all}
code{background:var(--accent-soft);padding:.1rem .35rem;border-radius:6px}
a{color:var(--accent)}
.stories h4{font-size:.8rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:1rem 0 .4rem}
.stories h4:first-child{margin-top:0}
.stories ol{margin:0;padding-left:1.3rem}
.stories li{border:0;padding:.2rem 0}
.stories li a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--line)}
.stories li a:hover{border-color:var(--accent)}
.src{display:block;color:var(--muted);font-size:.8rem}
.back{text-decoration:none;font-size:.9rem}
.transcript p{margin:0 0 1rem}
li .links{display:block;margin-top:.35rem;font-size:.85rem}
"""


def build_index(base):
    episodes = load_episodes()
    feed_url = "%s/feed.xml" % base

    if episodes:
        latest = episodes[0]
        body = stories_html(latest) or '<p class="summary">%s</p>' % escape(
            sentence_trim(latest.get("summary") or latest["description"], 700))

        transcript_btn = (
            '<a class="btn primary" href="episodes/%s.html">Read full transcript</a>' % latest["date"]
            if (EPISODES / (latest["date"] + ".html")).exists() else ""
        )

        latest_html = (
            '<section class="latest">'
            '<span class="label">Latest episode</span>'
            "<h2>%s</h2>"
            '<p class="meta">%s &middot; %s</p>'
            '<audio controls preload="metadata" src="episodes/%s.mp3"></audio>'
            "%s"
            '<div class="actions">%s'
            '<a class="btn" href="episodes/%s.mp3" download>Download MP3</a>'
            '<button class="btn" type="button" id="copy">Copy podcast feed link</button>'
            "</div></section>"
        ) % (
            escape(latest["title"]),
            pretty_date(latest["date"]),
            pretty_minutes(latest.get("duration")),
            latest["date"],
            body,
            transcript_btn,
            latest["date"],
        )
    else:
        latest_html = '<p class="empty">The first episode is on its way.</p>'

    def older_item(m):
        link = (
            '<span class="links"><a href="episodes/%s.html">Read transcript</a></span>' % m["date"]
            if (EPISODES / (m["date"] + ".html")).exists() else ""
        )
        return (
            "<li><strong>%s</strong><span>%s &middot; %s</span>"
            '<audio controls preload="none" src="episodes/%s.mp3"></audio>%s</li>'
            % (escape(m["title"]), pretty_date(m["date"]),
               pretty_minutes(m.get("duration")), m["date"], link)
        )

    older = "".join(older_item(m) for m in episodes[1:])

    older_html = ("<h3>Previous episodes</h3><ul>%s</ul>" % older) if older else ""

    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"icon\" href=\"cover.jpg\">"
        "<link rel=\"alternate\" type=\"application/rss+xml\" href=\"%s\">"
        "<title>%s</title><style>%s</style></head><body><div class=\"wrap\">"
        "<header><img src=\"cover.jpg\" alt=\"\"><div><h1>%s</h1><p>%s</p></div></header>"
        "%s%s"
        "<footer>Subscribe in any podcast app with <code>%s</code></footer>"
        "</div><script>"
        "var b=document.getElementById('copy');if(b){b.onclick=function(){"
        "navigator.clipboard.writeText('%s').then(function(){b.textContent='Copied';"
        "setTimeout(function(){b.textContent='Copy podcast feed link'},2000)})}}"
        "</script></body></html>"
    ) % (
        feed_url, escape(SHOW_TITLE), PAGE_STYLE, escape(SHOW_TITLE),
        escape(SHOW_DESCRIPTION), latest_html, older_html, feed_url, feed_url,
    )

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

    # Your own artwork in docs/cover.png wins; otherwise draw a default once.
    if (DOCS / "cover.png").exists():
        use_custom_cover(DOCS / "cover.png", DOCS / "cover.jpg")
    elif not (DOCS / "cover.jpg").exists():
        make_cover(DOCS / "cover.jpg")

    data = fetch_episode()
    date_text = data["date"]

    if (EPISODES / (date_text + ".json")).exists() and not FORCE:
        print("Episode %s is already published." % date_text)
    else:
        save_episode(date_text, clean_for_speech(data["script"]))
        prune()
        print("Episode %s ready." % date_text)

    write_extras(date_text, data)

    # Older episodes published before titles and transcripts existed.
    for meta in load_episodes():
        if meta["date"] == date_text:
            continue
        if meta.get("stories") and (EPISODES / (meta["date"] + ".html")).exists():
            continue
        older = fetch_episode(meta["date"])
        if older:
            write_extras(meta["date"], older)

    # Always rebuild the feed and page (nothing is committed if unchanged).
    base = base_url()

    build_feed(base)
    build_index(base)

    print("Feed: %s/feed.xml" % base)


if __name__ == "__main__":
    main()

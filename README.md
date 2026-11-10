# Physio Weekly for Sabaa

A short weekly podcast on the latest physiotherapy and rehabilitation research and technology.

- Every Monday morning the PhysioCollector Apps Script writes the episode script into the "Physio Weekly Podcast" Google Sheet.
- This repo's GitHub Action (Mondays 09:32 and 11:02 Tehran) fetches it, turns it into an MP3 with Edge TTS and updates the RSS feed in `docs/`.
- Feed: `https://shahriar-dadgar.github.io/physio-weekly-podcast/feed.xml`

Secrets needed (Settings → Secrets and variables → Actions): `APPS_SCRIPT_URL`, `PODCAST_TOKEN`.
GitHub Pages: Settings → Pages → Deploy from branch `main`, folder `/docs`.

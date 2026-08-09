# medrevue-calendar

Auto-syncs the MedRevue rehearsal schedule (from a Google Doc) into a calendar
feed anyone can subscribe to, plus a live landing page to browse it. Runs
hourly via GitHub Actions, parsed with Groq, published on GitHub Pages.

**Live site:** https://calendar.medrevue.animeisamistake.com/

## Subscribe

The landing page has a one-tap **Add to Calendar** button that detects your
device (Apple, Android, Windows, Linux) and opens the right subscribe flow
automatically. If you'd rather do it manually, this is the feed URL:

```
https://calendar.medrevue.animeisamistake.com/calendar.ics
```

- **Google Calendar**: Other calendars → **+** → *From URL* → paste the link above.
- **Apple Calendar**: File → *New Calendar Subscription* → paste the link above.
- **Outlook**: Add calendar → *Subscribe from web* → paste the link above.

Updates hourly. Google Calendar's own refresh interval is looser (often
12–24h) — the feed itself is always current if you hit the URL directly.

## What's on the site

- A **"next up"** card for the soonest rehearsal
- An **upcoming sessions** list (next 8, rebuilt every run so it's always
  relative to "now" — not just when the source doc changes)
- A **smart subscribe button** plus manual links for Apple/Google/Outlook/Linux
- A **copyable feed URL** and a direct `.ics` download
- **Light/dark/system theme toggle**

## How it works

- `scripts/sync_calendar.py` fetches the Google Doc, sends it to Groq in
  chunks to pull out structured events, merges duplicate write-ups of the
  same session, resolves venue names into something actually mappable, and
  writes `docs/calendar.ics` deterministically (the LLM only ever extracts
  fields — it never touches the `.ics` output directly, so a bad parse
  produces a wrong/missing event, not a corrupt file).
- `scripts/generate_landing_page.py` reads that `.ics` back and rebuilds
  `docs/index.html` — the landing page above — every run, since "what's
  upcoming" changes with the clock even when the doc hasn't.
- Location resolution is a separate, cached step: a free OpenStreetMap
  (Nominatim) geocode check first, then a web-search-enabled Groq lookup
  against Adelaide University's site if that doesn't resolve it. Results are
  cached in `docs/.location_cache.json`; anything it can't confidently
  resolve is left as the raw text with a note rather than a guessed address.
- `.github/workflows/update_calendar.yml` runs both scripts hourly and
  commits the result if anything changed.
- `docs/` is served by GitHub Pages, with `docs/CNAME` pointing it at
  `calendar.medrevue.animeisamistake.com`.
- The whole thing auto-retires after a cutoff date (currently end of 2026)
  so it doesn't keep running after the show's over — see `SETUP.md`.

## Setup / troubleshooting

See [`SETUP.md`](./SETUP.md) for the full walkthrough (Doc sharing, Groq
key, repo secrets, DNS, enabling Pages).
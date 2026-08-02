# medrevue-calendar

Auto-syncs the medrevue rehearsal schedule (from a Google Doc) into a calendar
feed anyone can subscribe to. Runs hourly via GitHub Actions, parsed with
Groq, published as a static `.ics` on GitHub Pages.

## Subscribe

```
https://calendar.medrevue.animeisamistake.com/calendar.ics
```

That's the feed. `https://calendar.medrevue.animeisamistake.com/` on its own
will 404 by design — there's a landing page there too
(`docs/index.html`) showing the next few rehearsals, but the URL above is
the one your calendar app actually needs.

- **Google Calendar**: Other calendars → **+** → *From URL* → paste the link above.
- **Apple Calendar**: File → *New Calendar Subscription* → paste the link above.
- **Outlook**: Add calendar → *Subscribe from web* → paste the link above.

Updates hourly. Google Calendar's own refresh interval is looser (often
12–24h) — the feed itself is always current if you hit the URL directly.

## How it works

- `scripts/sync_calendar.py` fetches the Google Doc, sends it to Groq to pull
  out structured events, and writes `docs/calendar.ics` deterministically
  (the LLM only extracts fields — it never touches the `.ics` output
  directly, so a bad parse produces a wrong/missing event, not a corrupt
  file).
- `.github/workflows/update_calendar.yml` runs that hourly and commits the
  result if it changed.
- `docs/` is served by GitHub Pages, with `docs/CNAME` pointing it at
  `calendar.medrevue.animeisamistake.com`.

## Setup / troubleshooting

See [`SETUP.md`](./SETUP.md) for the full walkthrough (Doc sharing, Groq
key, repo secrets, DNS, enabling Pages).
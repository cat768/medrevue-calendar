# AGENTS.md — MedRevue Rehearsal Calendar Sync

Context for any AI agent (Claude or otherwise) picking up work on this repo.
Read this before making changes.

## What this project does

Hourly pipeline: watches a Google Doc rehearsal schedule for changes, parses
it with an LLM into structured events, and rebuilds a public `.ics` calendar
feed that cast/crew subscribe to in Google/Apple Calendar.

Doc → GitHub Actions (cron) → Groq (parse) → `docs/calendar.ics` →
GitHub Pages → `calendar.medrevue.animeisamistake.com`

## Why it's built this way (don't relitigate these unless asked)

- **Zero budget.** Owner is not spending money on this. Every component was
  chosen because it's free: GitHub Actions free tier, GitHub Pages free tier,
  Groq free tier, and DNS on a domain (`animeisamistake.com`) already owned
  via Squarespace.
- **Public repo is required.** Free GitHub accounts can only serve Pages from
  public repos. This is fine — the calendar feed needs to be public anyway
  for calendar apps to subscribe to it. Repo secrets (`GROQ_API_KEY`,
  `GOOGLE_DOC_ID`) stay encrypted/masked regardless of repo visibility —
  do not assume they need extra protection beyond GitHub's normal secrets
  mechanism.
- **LLM only extracts JSON, never writes ICS directly.** The `.ics` file is
  built deterministically in Python (`scripts/sync_calendar.py`) from the
  LLM's structured output. This is intentional so a bad LLM response can
  only skip/mis-parse an event, never produce a malformed calendar file.
- **Change detection via content hash.** Groq is only called when the doc's
  text hash differs from the last committed hash (`docs/.last_hash`). Don't
  remove this — it's what keeps this well within Groq's free rate limits.
- **Stable UIDs.** Event UIDs are deterministic hashes of doc ID + date +
  time + title, so re-syncing updates existing events instead of
  duplicating them.
- **Hard cutoff date.** The sync intentionally goes inert after
  `CUTOFF_DATE` (currently 2026-12-31, set as an env var in
  `.github/workflows/update-calendar.yml`) so it doesn't run indefinitely
  after the show is over. If asked to "extend" or "stop" the project, adjust
  this value rather than changing the underlying logic.

## Key facts / config

- Google Doc ID: `1pPaUiMktu3jfMFhV9Pk1zLnw3ItfXSS_GkQUemP1y9M` (shared as
  "anyone with the link can view" — required for the no-auth export fetch)
- GitHub Pages hostname: `cat768.github.io`
- Custom domain: `calendar.medrevue.animeisamistake.com` (CNAME set up on
  Squarespace DNS, pointed at the GitHub Pages hostname above)
- Timezone: `Australia/Adelaide`
- LLM: Groq, model `openai/gpt-oss-120b` — Groq deprecates models
  periodically; if a run starts failing with a model-not-found error, check
  console.groq.com/docs/deprecations before assuming something else broke
- Feed URL (once live): `https://calendar.medrevue.animeisamistake.com/calendar.ics`

## Status / setup completed so far

- [x] Repo created and public, files pushed
- [x] Secrets added (`GOOGLE_DOC_ID`, `GROQ_API_KEY`)
- [x] GitHub Pages enabled, source = `main` / `/docs`
- [x] Squarespace CNAME added for `calendar.medrevue`
- [x] DNS check passed on GitHub Pages settings
- [ ] HTTPS enforcement confirmed enabled
- [ ] Feed verified subscribing correctly in Google/Apple Calendar
- [ ] First real rehearsal-schedule change tested end-to-end

## Things to be careful about

- Don't lower the copyright/quote-per-source type constraints — not
  applicable here, but *do* keep the "LLM never writes raw ICS" boundary;
  it's a deliberate reliability choice, not an oversight to "simplify away."
- Don't suggest converting to a private repo as a "more secure" option —
  it would break Pages on the free tier for no real security benefit (see
  above).
- If asked to add more calendars/shows, prefer parameterizing
  (`GOOGLE_DOC_ID`, `CALENDAR_NAME`, output path) over duplicating the
  workflow file.
# Rehearsal Schedule → Calendar Sync — Setup

Total cost: $0 (GitHub Actions free tier, GitHub Pages free tier, Groq free tier,
and DNS on a domain you already own).

## 1. Share the Google Doc
Open the doc → **Share** → **General access** → **Anyone with the link** → **Viewer**.
(The script fetches it via the public export URL, no OAuth needed. If you'd
rather keep it fully private, that requires the Google Docs API + a service
account instead — say the word and I'll swap that in.)

Grab the doc ID from the URL:
`https://docs.google.com/document/d/`**`1pPaUiMktu3jfMFhV9Pk1zLnw3ItfXSS_GkQUemP1y9M`**`/edit...`

## 2. Get a Groq API key
console.groq.com → API Keys → Create. Free tier, no card required (rate-limited).

## 3. Create a GitHub repo
- Push this folder's contents to a new repo, e.g. `medrevue-calendar`.
- Repo can be public or private — either way, well within the 2,000
  free Action-minutes/month (this job runs ~24 times/day at ~1-2 min each).

## 4. Add repo secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `GOOGLE_DOC_ID` = `1pPaUiMktu3jfMFhV9Pk1zLnw3ItfXSS_GkQUemP1y9M`
- `GROQ_API_KEY` = your Groq key

## 5. Enable GitHub Pages
Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch:
`main` / folder: `/docs` → Save.

GitHub will detect the `docs/CNAME` file and start provisioning the custom
domain + a free HTTPS certificate (can take a few minutes to a few hours).

## 6. Point your DNS at GitHub Pages
In Squarespace's domain DNS settings (Domains → animeisamistake.com → DNS),
add:

| Type  | Host                | Data                        |
|-------|---------------------|------------------------------|
| CNAME | `calendar.medrevue` | `<your-github-username>.github.io` |

This only touches the `calendar.medrevue` subdomain — it won't affect your
main Squarespace site.

## 7. First run
Go to the repo's **Actions** tab → "Update Rehearsal Calendar" → **Run workflow**
to trigger it manually the first time. After that it runs automatically every
hour via cron.

## 8. Subscribe to it
Once DNS + Pages propagate, the live calendar feed is:

```
https://calendar.medrevue.animeisamistake.com/calendar.ics
```

- **Google Calendar**: Other calendars → + → "From URL" → paste the link.
- **Apple Calendar**: File → New Calendar Subscription → paste the link.
- Google Calendar's subscription refresh interval isn't guaranteed to be
  hourly on its end (it's often more like every 12–24h) — but the underlying
  .ics file itself updates hourly, and anyone hitting the URL directly always
  gets the latest version.

## Auto-stop at end of 2026
`sync_calendar.py` checks the date on every run and, once it's past
`CUTOFF_DATE` (set to **2026-12-31** in the workflow), it does nothing —
no doc fetch, no Groq call, no calendar edits — and just logs that it's
retired. You don't need to remember to turn anything off.

The workflow itself keeps firing hourly forever (a few seconds of runtime
each time, effectively free even on the private-repo tier), it just becomes
a no-op after the cutoff. If you'd rather it stop running entirely, either:
- delete `.github/workflows/update-calendar.yml` from the repo, or
- Repo → Actions → "Update Rehearsal Calendar" → **⋯ → Disable workflow**.

To change the cutoff later, edit the `CUTOFF_DATE` line in
`.github/workflows/update-calendar.yml`.

## Notes / things worth double-checking
- **Model choice**: the script uses `openai/gpt-oss-120b`, Groq's current
  recommended general-purpose model as of mid-2026. If Groq deprecates it
  later, swap the `GROQ_MODEL` constant in `scripts/sync_calendar.py`.
- **Timezone**: defaults to `Australia/Adelaide` — change the `TIMEZONE` env
  var in the workflow if the rehearsals are actually elsewhere.
- **Parsing quality**: the LLM step only extracts structured fields (date,
  time, title, location, notes) — the actual .ics file is built
  deterministically in Python, so a parsing hiccup can't produce a broken
  calendar file, just a skipped/wrong event you can catch by eyeballing
  `docs/calendar.ics` in the repo.
- **Free tier limits**: Groq's free tier is rate-limited but has no spend
  cap; for one document parsed at most once an hour this is nowhere near the
  limits. Worth a glance at console.groq.com if you ever see failures.
- **Location resolution**: ambiguous venue names (nicknames, room codes) get
  looked up automatically — a free OpenStreetMap geocode check first, then a
  web-search-enabled Groq call against Adelaide University's site if that
  doesn't resolve it. No extra setup or secrets needed, it reuses
  `GROQ_API_KEY`. Results are cached in `docs/.location_cache.json`; if a
  location's `LOCATION` field in the calendar ever looks wrong, that file is
  the place to check (or just delete the relevant entry to force a re-lookup
  next run). Locations it couldn't confidently resolve are left as the raw
  text with a note in the event description rather than a guessed address.
- **Progress bars**: the Actions log now shows `tqdm` progress bars for both
  the per-chunk Groq parsing and the location-resolution step, so you can
  tell at a glance how far a run has gotten.

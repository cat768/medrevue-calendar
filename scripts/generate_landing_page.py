#!/usr/bin/env python3
"""
Render docs/index.html: a self-contained landing page for the rehearsal
calendar feed. Reads the already-built docs/calendar.ics (source of truth --
this script never talks to the Google Doc or Groq, it just re-renders
whatever sync_calendar.py last wrote) and produces:

  - a "next up" card for the soonest session
  - an "Upcoming sessions" list (every session that hasn't finished yet)
  - a "Past sessions" list, collapsed by default, pinned to the very bottom
    of the page (below the subscribe/feed sections) -- this is what makes
    the page useful for sanity-checking a sync: every event the feed has
    ever produced is on the page somewhere, split cleanly by whether it's
    still ahead of us or already happened, each one dated with its year so
    nothing is ambiguous across a year boundary.
  - a subscribe button that adapts to the visitor's device (Apple Calendar /
    Google Calendar / Outlook), detected client-side in the browser
  - a read-only feed URL box with a copy button, plus a manual download link

Runs every workflow execution (not just when the doc changes) because
"upcoming vs past" depends on the current time, not just on the doc content.

Optional env vars (mirror sync_calendar.py where they overlap):
  FEED_URL        - absolute HTTPS URL of the .ics feed, default the
                    production medrevue URL
  TIMEZONE        - IANA tz name, default "Australia/Adelaide"
  CALENDAR_NAME   - fallback display name if the ICS has no X-WR-CALNAME,
                    default "Rehearsal Schedule"
  UPCOMING_LIMIT  - how many upcoming sessions to list, default 0 (unlimited
                    -- show every upcoming session, since this page doubles
                    as the sanity-check view for the sync)
  PAST_LIMIT      - how many past sessions to list (most recent first),
                    default 0 (unlimited). Past sessions live in a collapsed
                    <details> section so an unlimited count doesn't bloat
                    the page a first-time visitor sees.
  GITHUB_URL      - repo link for the footer, default cat768/medrevue-calendar
"""

import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from icalendar import Calendar

ICS_FILE = "docs/calendar.ics"
OUTPUT_FILE = "docs/index.html"

FEED_URL = os.environ.get(
    "FEED_URL", "https://calendar.medrevue.animeisamistake.com/calendar.ics"
)
TZ_NAME = os.environ.get("TIMEZONE", "Australia/Adelaide")
DEFAULT_CAL_NAME = os.environ.get("CALENDAR_NAME", "Rehearsal Schedule")
UPCOMING_LIMIT = int(os.environ.get("UPCOMING_LIMIT", "0"))
PAST_LIMIT = int(os.environ.get("PAST_LIMIT", "0"))
GITHUB_URL = os.environ.get("GITHUB_URL", "https://github.com/cat768/medrevue-calendar")


# ---------------------------------------------------------------- parsing --

def load_events(
    ics_path: str, tz_name: str
) -> tuple[list[dict], list[dict], str | None]:
    """Returns (upcoming, past, calendar_name).

    Every VEVENT in the feed is kept (nothing is silently dropped, which is
    the whole point of the split -- a maintainer sanity-checking a sync run
    should be able to see the full history, not just what's ahead). Each
    event is bucketed by whether it has already finished as of "now":

      - upcoming: sorted ascending (soonest first), same as before.
      - past:     sorted descending (most recently finished first), since
                  that's what you want at a glance when checking "did the
                  last few sessions come through correctly".

    calendar_name is None if the file is missing (first-run / not synced
    yet).
    """
    if not os.path.exists(ics_path):
        return [], [], None

    with open(ics_path, "rb") as f:
        cal = Calendar.from_ical(f.read())

    cal_name = str(cal.get("x-wr-calname") or "") or None
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    upcoming: list[dict] = []
    past: list[dict] = []
    for comp in cal.walk("VEVENT"):
        dtstart_prop = comp.get("dtstart")
        if dtstart_prop is None:
            continue
        start = dtstart_prop.dt
        if not isinstance(start, datetime):
            continue  # skip bare-date all-day entries, this feed doesn't use them
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        else:
            start = start.astimezone(tz)

        dtend_prop = comp.get("dtend")
        end = start
        if dtend_prop is not None:
            end_dt = dtend_prop.dt
            if isinstance(end_dt, datetime):
                end = end_dt.replace(tzinfo=tz) if end_dt.tzinfo is None else end_dt.astimezone(tz)

        event = {
            "start": start,
            "end": end,
            "title": str(comp.get("summary") or "Rehearsal").strip(),
            "location": str(comp.get("location") or "").strip(),
            "notes": str(comp.get("description") or "").strip(),
        }

        if end < now:
            past.append(event)
        else:
            upcoming.append(event)

    upcoming.sort(key=lambda e: e["start"])
    past.sort(key=lambda e: e["start"], reverse=True)
    return upcoming, past, cal_name


# --------------------------------------------------------------- rendering --

def fmt_time(dt: datetime) -> str:
    s = dt.strftime("%I:%M %p")
    if s.startswith("0"):
        s = s[1:]
    return s.lower()


def truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())  # collapse embedded newlines/whitespace
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def render_next_up(event: dict | None) -> str:
    if event is None:
        return ""
    start = event["start"]
    location_html = (
        f'<div class="stub-location">{html.escape(event["location"])}</div>'
        if event["location"] else ""
    )
    return f"""
    <section class="next-up" aria-label="Next session">
      <div class="stub">
        <span class="stub-tag">Next up</span>
        <div class="stub-when">
          <span class="stub-day">{html.escape(start.strftime("%a").upper())}</span>
          <span class="stub-date">{html.escape(start.strftime("%-d %b %Y").upper())}</span>
        </div>
        <div class="stub-time">{html.escape(fmt_time(event["start"]))} \u2013 {html.escape(fmt_time(event["end"]))}</div>
        <h2 class="stub-title">{html.escape(event["title"])}</h2>
        {location_html}
      </div>
    </section>"""


def render_schedule_rows(events: list[dict], variant: str = "upcoming") -> str:
    """variant is "upcoming" or "past" -- controls the empty-state copy and
    adds a dimming modifier class to past rows so the two sections are
    visually distinct even before you notice which heading you're under."""
    if not events:
        if variant == "past":
            return """
        <p class="empty-state">No past sessions yet \u2014 nothing on the
        schedule has happened yet.</p>"""
        return """
        <p class="empty-state">No sessions on the schedule right now. The feed
        is still live \u2014 check back after the next sync, or flag it to the
        committee if the doc hasn't moved in a while.</p>"""

    row_class = "row row--past" if variant == "past" else "row"
    rows = []
    for ev in events:
        start = ev["start"]
        meta_bits = []
        if ev["location"]:
            meta_bits.append(f'<span class="row-location">{html.escape(ev["location"])}</span>')
        if ev["notes"]:
            meta_bits.append(f'<span class="row-notes">{html.escape(truncate(ev["notes"]))}</span>')
        meta_html = "".join(f"<div>{b}</div>" for b in meta_bits)

        rows.append(f"""
        <li class="{row_class}">
          <div class="row-date">
            <span class="row-day">{html.escape(start.strftime("%a").upper())}</span>
            <span class="row-daynum">{html.escape(start.strftime("%-d"))}</span>
            <span class="row-month">{html.escape(start.strftime("%b").upper())}</span>
            <span class="row-year">{html.escape(start.strftime("%Y"))}</span>
          </div>
          <div class="row-time">{html.escape(fmt_time(ev["start"]))}<br>\u2013 {html.escape(fmt_time(ev["end"]))}</div>
          <div class="row-details">
            <div class="row-title">{html.escape(ev["title"])}</div>
            {meta_html}
          </div>
        </li>""")

    schedule_class = "schedule schedule--past" if variant == "past" else "schedule"
    return f'<ul class="{schedule_class}">{"".join(rows)}</ul>'


def render_limit_note(shown: int, total: int, limit: int) -> str:
    """Small note when a LIMIT env var actually truncated the list, so it's
    obvious on the page (not just in the workflow logs) that you're not
    looking at everything."""
    if limit <= 0 or total <= shown:
        return ""
    return (
        f'<p class="limit-note">Showing {shown} of {total} \u2014 '
        f'the rest are still in the feed.</p>'
    )


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__CAL_NAME__ \u2014 MedRevue</title>
<meta name="description" content="Subscribe to the MedRevue rehearsal calendar feed and see upcoming sessions.">
<script>
(function () {
  try {
    var t = localStorage.getItem("medrevue-theme");
    if (t === "dark" || t === "light") {
      document.documentElement.setAttribute("data-theme", t);
    }
  } catch (e) {}
})();
</script>
<style>
  :root {
    --paper: #fdfcfa;
    --paper-dim: #f4f0e6;
    --ink: #17140f;
    --ink-dim: #5b5648;
    --rule: #ddd6c4;
    --curtain: #8c1d2b;
    --curtain-dark: #6b1520;
    --gold: #a9822f;
    --on-curtain: #fdfcfa;
    --radius: 10px;
    --max-w: 720px;
    --font-display: Georgia, "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  /* Explicit dark theme (user picked "Dark") */
  :root[data-theme="dark"] {
    --paper: #16140f;
    --paper-dim: #211e17;
    --ink: #f2ede0;
    --ink-dim: #b3ab97;
    --rule: #3a3527;
    --curtain: #a5283a;
    --curtain-dark: #d6495c;
  }
  /* "System" (no explicit choice saved) follows the OS preference */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]):not([data-theme="dark"]) {
      --paper: #16140f;
      --paper-dim: #211e17;
      --ink: #f2ede0;
      --ink-dim: #b3ab97;
      --rule: #3a3527;
      --curtain: #a5283a;
      --curtain-dark: #d6495c;
    }
  }
  * { box-sizing: border-box; }
  .sr-only {
    position: absolute;
    width: 1px; height: 1px;
    padding: 0; margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  html { color-scheme: light dark; }
  :root[data-theme="light"] { color-scheme: light; }
  :root[data-theme="dark"] { color-scheme: dark; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: var(--font-sans);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    transition: background 0.15s ease, color 0.15s ease;
  }
  main {
    max-width: var(--max-w);
    margin: 0 auto;
    padding: 48px 20px 64px;
  }
  header.page-head {
    margin-bottom: 32px;
  }
  .head-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .theme-toggle {
    display: inline-flex;
    flex-shrink: 0;
    border: 1px solid var(--rule);
    border-radius: 8px;
    padding: 2px;
    gap: 2px;
    background: var(--paper-dim);
  }
  .theme-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--ink-dim);
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .theme-btn svg { width: 15px; height: 15px; display: block; }
  .theme-btn:hover { color: var(--ink); }
  .theme-btn[aria-pressed="true"] {
    background: var(--paper);
    color: var(--curtain-dark);
  }
  .theme-btn:focus-visible {
    outline: 2px solid var(--curtain);
    outline-offset: 1px;
  }
  .eyebrow {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.18em;
    color: var(--curtain);
    text-transform: uppercase;
    margin: 0 0 10px;
  }
  h1 {
    font-family: var(--font-display);
    font-weight: 700;
    font-size: clamp(28px, 6vw, 40px);
    line-height: 1.1;
    margin: 0 0 10px;
    letter-spacing: -0.01em;
  }
  .subhead {
    color: var(--ink-dim);
    font-size: 16px;
    margin: 0;
    max-width: 52ch;
  }

  /* --- next up ticket stub --- */
  .next-up { margin: 32px 0; }
  .stub {
    position: relative;
    background: var(--curtain);
    color: var(--on-curtain);
    border-radius: var(--radius);
    padding: 22px 22px 30px;
    transform: rotate(-0.6deg);
    animation: settle 0.5s ease-out;
  }
  .stub::after {
    content: "";
    position: absolute;
    left: 0; right: 0; bottom: 0;
    height: 14px;
    background-image: radial-gradient(circle, var(--paper) 3px, transparent 3.4px);
    background-size: 16px 16px;
    background-position: 8px 3px;
    background-repeat: repeat-x;
  }
  .stub-tag {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    background: var(--paper);
    color: var(--curtain-dark);
    padding: 3px 9px;
    border-radius: 3px;
    transform: rotate(-2deg);
  }
  .stub-when {
    margin-top: 14px;
    font-family: var(--font-mono);
    font-weight: 700;
  }
  .stub-day { font-size: 14px; letter-spacing: 0.1em; opacity: 0.85; margin-right: 8px; }
  .stub-date { font-size: 20px; letter-spacing: 0.04em; }
  .stub-time {
    font-family: var(--font-mono);
    font-size: 15px;
    opacity: 0.9;
    margin-top: 4px;
  }
  .stub-title {
    font-family: var(--font-display);
    font-size: 22px;
    margin: 10px 0 2px;
  }
  .stub-location { font-size: 14px; opacity: 0.88; }

  @keyframes settle {
    from { opacity: 0; transform: translateY(8px) rotate(-0.6deg); }
    to   { opacity: 1; transform: translateY(0) rotate(-0.6deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .stub { animation: none; }
  }

  /* --- subscribe --- */
  section.subscribe { margin: 36px 0; }
  h2 {
    font-family: var(--font-display);
    font-size: 20px;
    margin: 0 0 12px;
  }
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 100%;
    padding: 15px 20px;
    background: var(--curtain);
    color: var(--on-curtain);
    font-family: var(--font-sans);
    font-size: 16px;
    font-weight: 600;
    text-decoration: none;
    border: none;
    border-radius: var(--radius);
    cursor: pointer;
    transition: background 0.15s ease;
  }
  .btn:hover { background: var(--curtain-dark); }
  .btn:focus-visible, a:focus-visible, input:focus-visible, button:focus-visible {
    outline: 2px solid var(--curtain);
    outline-offset: 2px;
  }
  .alt-line {
    margin-top: 12px;
    font-size: 13px;
    color: var(--ink-dim);
  }
  .alt-line a { color: var(--ink-dim); }
  .alt-links {
    display: flex;
    flex-wrap: wrap;
    row-gap: 4px;
    column-gap: 14px;
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: 13px;
  }
  .alt-links a { color: var(--curtain-dark); text-decoration: none; border-bottom: 1px solid var(--rule); }
  .alt-links a:hover { border-color: var(--curtain-dark); }

  /* --- schedule --- */
  section.schedule-section { margin: 40px 0; }
  .section-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }
  .section-head h2 { margin: 0; }
  .section-count {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--ink-dim);
    letter-spacing: 0.03em;
    white-space: nowrap;
  }
  .limit-note {
    color: var(--ink-dim);
    font-size: 12.5px;
    margin: 10px 0 0;
  }
  .schedule { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--rule); }
  .row {
    display: grid;
    grid-template-columns: 58px 64px 1fr;
    gap: 14px;
    align-items: start;
    padding: 14px 0;
    border-bottom: 1px solid var(--rule);
  }
  .row-date {
    display: flex;
    flex-direction: column;
    font-family: var(--font-mono);
    line-height: 1.2;
  }
  .row-day { font-size: 11px; color: var(--ink-dim); letter-spacing: 0.06em; }
  .row-daynum { font-size: 20px; font-weight: 700; }
  .row-month { font-size: 11px; color: var(--ink-dim); letter-spacing: 0.06em; }
  .row-year { font-size: 10px; color: var(--ink-dim); letter-spacing: 0.03em; margin-top: 1px; opacity: 0.8; }
  .row-time {
    font-family: var(--font-mono);
    font-size: 12.5px;
    color: var(--ink-dim);
    line-height: 1.4;
  }
  .row-title { font-weight: 600; font-size: 15px; }
  .row-location {
    display: inline-block;
    font-size: 13px;
    color: var(--curtain-dark);
    margin-top: 2px;
  }
  .row-notes {
    display: block;
    font-size: 13px;
    color: var(--ink-dim);
    font-style: italic;
    margin-top: 2px;
  }
  .empty-state { color: var(--ink-dim); font-size: 15px; }

  /* past rows: same layout, visually receded so the eye lands on what's
     still ahead first, even if you've expanded the past section */
  .row--past { opacity: 0.66; }
  .row--past .row-title { font-weight: 500; }
  .row--past .row-location { color: var(--ink-dim); }

  /* --- past sessions (collapsed by default, pinned at the bottom) --- */
  section.past-section { margin: 40px 0 8px; }
  section.past-section summary {
    cursor: pointer;
    font-family: var(--font-display);
    font-size: 20px;
    color: var(--ink);
    padding: 4px 0;
    list-style: none;
  }
  section.past-section summary::-webkit-details-marker { display: none; }
  section.past-section summary::before {
    content: "\\25B8";
    display: inline-block;
    margin-right: 8px;
    font-size: 15px;
    color: var(--ink-dim);
    transition: transform 0.15s ease;
  }
  section.past-section details[open] summary::before { transform: rotate(90deg); }
  section.past-section summary .section-count { margin-left: 10px; }
  section.past-section summary:focus-visible {
    outline: 2px solid var(--curtain);
    outline-offset: 2px;
  }
  section.past-section .schedule { margin-top: 14px; }

  /* --- feed url --- */
  section.feed-section { margin: 40px 0 8px; }
  .url-box {
    display: flex;
    gap: 8px;
    background: var(--paper-dim);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 6px 6px 6px 14px;
    align-items: center;
  }
  .url-box input {
    flex: 1;
    min-width: 0;
    border: none;
    background: transparent;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--ink);
    padding: 8px 0;
  }
  .url-box button {
    flex-shrink: 0;
    font-family: var(--font-sans);
    font-size: 13px;
    font-weight: 600;
    color: var(--paper);
    background: var(--ink);
    border: none;
    border-radius: 6px;
    padding: 9px 14px;
    cursor: pointer;
  }
  .url-box button:hover { background: var(--curtain-dark); }
  .feed-actions {
    margin-top: 10px;
    font-size: 13px;
  }
  .feed-actions a { color: var(--curtain-dark); }
  .helper-text {
    color: var(--ink-dim);
    font-size: 13px;
    margin: 8px 0 0;
  }

  footer {
    margin-top: 48px;
    padding-top: 18px;
    border-top: 1px solid var(--rule);
    font-size: 12.5px;
    color: var(--ink-dim);
  }
  footer a { color: var(--ink-dim); }

  @media (max-width: 420px) {
    .row { grid-template-columns: 46px 58px 1fr; }
  }
</style>
</head>
<body>
<main>
  <header class="page-head">
    <div class="head-row">
      <p class="eyebrow">MedRevue</p>
      <div class="theme-toggle" role="group" aria-label="Theme">
        <button type="button" class="theme-btn" data-theme-choice="light" aria-pressed="false" title="Light">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><line x1="12" y1="2" x2="12" y2="4"></line><line x1="12" y1="20" x2="12" y2="22"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="2" y1="12" x2="4" y2="12"></line><line x1="20" y1="12" x2="22" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
          <span class="sr-only">Light</span>
        </button>
        <button type="button" class="theme-btn" data-theme-choice="dark" aria-pressed="false" title="Dark">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
          <span class="sr-only">Dark</span>
        </button>
        <button type="button" class="theme-btn" data-theme-choice="system" aria-pressed="true" title="Match system">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
          <span class="sr-only">System</span>
        </button>
      </div>
    </div>
    <h1>__CAL_NAME__</h1>
    <p class="subhead">Auto-synced from the cast schedule doc. Subscribe once and every rehearsal shows up on its own \u2014 no more scrolling the group chat for a call time.</p>
  </header>

  __NEXT_UP_BLOCK__

  <section class="subscribe">
    <h2>Subscribe</h2>
    <a id="subscribe-btn" class="btn" href="__GCAL_URL__">Add to Calendar</a>
    <p class="alt-line">Picked for <span id="platform-name">your device</span> automatically \u2014 wrong guess?</p>
    <p class="alt-links">
      <a href="__WEBCAL_URL__">Apple Calendar</a>
      <a href="__GCAL_URL__">Google Calendar</a>
      <a href="__OUTLOOK_URL__">Outlook</a>
      <a href="__WEBCAL_URL__">Linux (GNOME Calendar)</a>
    </p>
    <noscript><p class="helper-text">JavaScript is off, so pick the right app above manually, or copy the feed URL below into any calendar app's "subscribe by URL" option.</p></noscript>
  </section>

  <section class="schedule-section">
    <div class="section-head">
      <h2>Upcoming sessions</h2>
      <span class="section-count">__UPCOMING_COUNT__</span>
    </div>
    __SCHEDULE_ROWS__
    __UPCOMING_LIMIT_NOTE__
  </section>

  <section class="feed-section">
    <h2>Feed URL</h2>
    <div class="url-box">
      <input id="feed-url" type="text" readonly value="__FEED_URL__" aria-label="Calendar feed URL">
      <button id="copy-btn" type="button">Copy</button>
    </div>
    <p class="helper-text">Paste this into any calendar app's "subscribe from URL" option. Updates hourly, straight from the feed \u2014 no need to re-download it.</p>
    <p class="feed-actions"><a id="copy-link" href="#">Copy link</a> \u00b7 <a href="calendar.ics" download>Download .ics file</a></p>
  </section>

  <section class="past-section">
    <details>
      <summary>Past sessions <span class="section-count">__PAST_COUNT__</span></summary>
      __PAST_ROWS__
      __PAST_LIMIT_NOTE__
    </details>
  </section>

  <footer>
    Synced automatically from the cast schedule doc, published on GitHub Pages. \u00b7
    <a href="__GITHUB_URL__">medrevue-calendar on GitHub</a>
  </footer>
</main>

<script>
(function () {
  var FEED_URL = "__FEED_URL__";
  var WEBCAL_URL = "__WEBCAL_URL__";
  var GCAL_URL = "__GCAL_URL__";
  var OUTLOOK_URL = "__OUTLOOK_URL__";

  function detectPlatform() {
    var ua = navigator.userAgent || "";
    var platform = navigator.platform || "";
    var isIOS = /iPad|iPhone|iPod/.test(ua) ||
      (platform === "MacIntel" && navigator.maxTouchPoints > 1);
    var isMac = /Mac/.test(platform) && !isIOS;
    var isAndroid = /Android/.test(ua);
    var isWindows = /Win/.test(platform);
    var isLinux = !isAndroid && (/Linux/.test(platform) || /X11/.test(ua));
    if (isIOS || isMac) return "apple";
    if (isAndroid) return "android";
    if (isWindows) return "windows";
    if (isLinux) return "linux";
    return "other";
  }

  var CONFIG = {
    apple:   { label: "Add to Apple Calendar",   href: WEBCAL_URL,  name: "Apple" },
    android: { label: "Add to Google Calendar",  href: GCAL_URL,    name: "Android" },
    windows: { label: "Add to Outlook",          href: OUTLOOK_URL, name: "Windows" },
    linux:   { label: "Add to Calendar (GNOME)", href: WEBCAL_URL,  name: "Linux" },
    other:   { label: "Add to Google Calendar",  href: GCAL_URL,    name: "your device" }
  };

  var choice = CONFIG[detectPlatform()];
  var btn = document.getElementById("subscribe-btn");
  var nameEl = document.getElementById("platform-name");
  if (btn) {
    btn.textContent = choice.label;
    btn.setAttribute("href", choice.href);
  }
  if (nameEl) nameEl.textContent = choice.name;

  var copyBtn = document.getElementById("copy-btn");
  var copyLink = document.getElementById("copy-link");
  var urlInput = document.getElementById("feed-url");

  function resetCopyLabel() {
    setTimeout(function () { if (copyBtn) copyBtn.textContent = "Copy"; }, 1800);
  }

  function fallbackCopy() {
    if (!urlInput) return;
    urlInput.removeAttribute("readonly");
    urlInput.focus();
    urlInput.select();
    urlInput.setSelectionRange(0, FEED_URL.length);
    try {
      document.execCommand("copy");
      if (copyBtn) copyBtn.textContent = "Copied";
    } catch (e) {
      if (copyBtn) copyBtn.textContent = "Select & Ctrl/Cmd+C";
    }
    urlInput.setAttribute("readonly", "readonly");
    resetCopyLabel();
  }

  function doCopy() {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(FEED_URL).then(function () {
        if (copyBtn) copyBtn.textContent = "Copied";
        resetCopyLabel();
      }, fallbackCopy);
    } else {
      fallbackCopy();
    }
  }

  if (copyBtn) copyBtn.addEventListener("click", doCopy);
  if (copyLink) copyLink.addEventListener("click", function (e) { e.preventDefault(); doCopy(); });
  if (urlInput) urlInput.addEventListener("click", function () { urlInput.select(); });

  // --- theme: light / dark / system (default) ---
  var THEME_KEY = "medrevue-theme";
  var root = document.documentElement;
  var themeButtons = document.querySelectorAll(".theme-btn");

  function getStoredTheme() {
    try {
      var t = localStorage.getItem(THEME_KEY);
      return (t === "light" || t === "dark") ? t : "system";
    } catch (e) {
      return "system";
    }
  }

  function applyTheme(pref) {
    if (pref === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", pref);
    }
    for (var i = 0; i < themeButtons.length; i++) {
      var b = themeButtons[i];
      b.setAttribute("aria-pressed", b.getAttribute("data-theme-choice") === pref ? "true" : "false");
    }
  }

  function setTheme(pref) {
    try {
      if (pref === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, pref);
    } catch (e) {}
    applyTheme(pref);
  }

  for (var i = 0; i < themeButtons.length; i++) {
    themeButtons[i].addEventListener("click", function () {
      setTheme(this.getAttribute("data-theme-choice"));
    });
  }

  applyTheme(getStoredTheme());

  var darkMql = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
  if (darkMql && darkMql.addEventListener) {
    darkMql.addEventListener("change", function () {
      if (getStoredTheme() === "system") applyTheme("system");
    });
  }
})();
</script>
</body>
</html>
"""


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def build_html(upcoming: list[dict], past: list[dict], cal_name: str) -> str:
    webcal_url = "webcal://" + FEED_URL.split("://", 1)[-1]
    gcal_url = "https://calendar.google.com/calendar/render?cid=" + _urlquote(FEED_URL)
    outlook_url = (
        "https://outlook.live.com/calendar/0/addfromweb?url="
        + _urlquote(FEED_URL) + "&name=" + _urlquote(cal_name)
    )

    upcoming_shown = upcoming[:UPCOMING_LIMIT] if UPCOMING_LIMIT > 0 else upcoming
    past_shown = past[:PAST_LIMIT] if PAST_LIMIT > 0 else past

    next_up = render_next_up(upcoming[0] if upcoming else None)
    upcoming_rows = render_schedule_rows(upcoming_shown, variant="upcoming")
    past_rows = render_schedule_rows(past_shown, variant="past")
    upcoming_limit_note = render_limit_note(len(upcoming_shown), len(upcoming), UPCOMING_LIMIT)
    past_limit_note = render_limit_note(len(past_shown), len(past), PAST_LIMIT)

    page = PAGE_TEMPLATE
    page = page.replace("__CAL_NAME__", html.escape(cal_name))
    page = page.replace("__NEXT_UP_BLOCK__", next_up)
    page = page.replace("__SCHEDULE_ROWS__", upcoming_rows)
    page = page.replace("__UPCOMING_LIMIT_NOTE__", upcoming_limit_note)
    page = page.replace("__UPCOMING_COUNT__", html.escape(_plural(len(upcoming), "session")))
    page = page.replace("__PAST_ROWS__", past_rows)
    page = page.replace("__PAST_LIMIT_NOTE__", past_limit_note)
    page = page.replace("__PAST_COUNT__", html.escape(_plural(len(past), "session")))
    page = page.replace("__FEED_URL__", html.escape(FEED_URL))
    page = page.replace("__WEBCAL_URL__", html.escape(webcal_url))
    page = page.replace("__GCAL_URL__", html.escape(gcal_url))
    page = page.replace("__OUTLOOK_URL__", html.escape(outlook_url))
    page = page.replace("__GITHUB_URL__", html.escape(GITHUB_URL))
    return page


def _urlquote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def main():
    upcoming, past, cal_name = load_events(ICS_FILE, TZ_NAME)
    page = build_html(upcoming, past, cal_name or DEFAULT_CAL_NAME)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(page)
    print(
        f"Wrote {OUTPUT_FILE}: {len(upcoming)} upcoming session(s), "
        f"{len(past)} past session(s)."
    )


if __name__ == "__main__":
    main()
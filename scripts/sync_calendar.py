#!/usr/bin/env python3
"""
Fetch a public Google Doc, detect changes since last run, parse the rehearsal
schedule with Groq, and (re)build an ICS calendar file.

Required env vars:
  GOOGLE_DOC_ID   - the ID from the doc URL (the long string between /d/ and /edit)
  GROQ_API_KEY    - your Groq API key
Optional env vars:
  TIMEZONE        - IANA tz name, default "Australia/Adelaide"
  CALENDAR_NAME   - display name for the calendar, default "Rehearsal Schedule"
  CUTOFF_DATE     - "YYYY-MM-DD" after which the sync stops doing anything,
                    default "2026-12-31"
"""

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar, Event

STATE_FILE = "docs/.last_hash"
ICS_FILE = "docs/calendar.ics"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"  # current Groq general-purpose model (2026)

# Groq's free tier caps openai/gpt-oss-120b at 8,000 tokens/minute, shared
# between input + reserved output, PER REQUEST. Rather than truncate the doc,
# we split it into chunks small enough that each request comfortably fits,
# and merge the results. This scales as the season's schedule grows instead
# of breaking once the doc passes a few thousand tokens.
CHUNK_MAX_CHARS = 4000
MAX_COMPLETION_TOKENS = 2048
CHUNK_DELAY_SECONDS = 12  # spacing between chunk calls, stays under free-tier RPM/TPM

SYSTEM_PROMPT = """You extract rehearsal schedule events from raw text copied from a \
Google Doc. Output ONLY a JSON array, no prose, no markdown fences.

Each element must have exactly these keys:
  "date": "YYYY-MM-DD"
  "start_time": "HH:MM" (24-hour)
  "end_time": "HH:MM" (24-hour) - if not stated, estimate a sensible duration \
or reuse start_time + 2 hours
  "title": short string, e.g. "Full Cast Rehearsal" or "Act 2 Scene 3"
  "location": string, empty string if not given
  "notes": string, empty string if not given

Rules:
- If the year is not stated, assume the nearest upcoming occurrence of that \
month/day relative to today.
- Skip rows that are clearly headers, not actual scheduled sessions.
- If you cannot find any valid events, output an empty JSON array: []
- Do not invent events that are not supported by the text.
- IMPORTANT: you are being shown one fragment of a larger document, not the \
whole thing. Only extract events that are fully described within this \
fragment (a clear date/time/location). If a fragment starts or ends mid-table \
or mid-sentence, ignore that partial row rather than guessing its contents.
"""


def fetch_doc_text(doc_id: str) -> str:
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch doc (status {resp.status_code}). "
            "Is it shared as 'Anyone with the link can view'?"
        )
    return resp.text


# The doc has a compact date/time/location summary up top, followed by
# exhaustive per-rehearsal cast/song/blocking breakdowns the calendar doesn't
# need. Sending all of it burns most of the free-tier TPM budget on tokens
# that never affect the output. Cut at the first detailed-rehearsal heading;
# if the doc gets restructured and the marker disappears, fall back to the
# full text rather than silently dropping something we don't recognize.
DETAIL_SECTION_MARKER = "Rehearsal #1"


def trim_to_summary(doc_text: str) -> str:
    idx = doc_text.find(DETAIL_SECTION_MARKER)
    if idx == -1:
        return doc_text
    return doc_text[:idx]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_last_hash() -> str | None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return f.read().strip()
    return None


def save_hash(h: str) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(h)


def call_groq(doc_text: str, api_key: str, today: str) -> list:
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        # Free-tier TPM for this model is tight (~8K tokens/min per Groq's
        # published limits) and this reservation counts against it up front,
        # on top of input tokens. Keep it modest — trim_to_summary() keeps
        # input small enough that this is still plenty for the JSON output.
        "max_completion_tokens": 4096,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Today's date is {today}.\n\nDoc text:\n{doc_text}",
            },
        ],
    }
    resp = requests.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        # Surface Groq's actual error body (e.g. exact TPM "Requested vs
        # Limit" numbers) instead of a bare status code — saves guessing.
        raise RuntimeError(
            f"Groq API error {resp.status_code}: {resp.text[:1000]}"
        )
    raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Defensive cleanup in case the model wraps output in fences anyway
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        events = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Groq did not return valid JSON: {e}\nRaw: {raw[:500]}")

    if not isinstance(events, list):
        raise RuntimeError("Groq output was valid JSON but not a list")
    return events


def build_ics(events: list, tz_name: str, cal_name: str, doc_id: str) -> bytes:
    tz = ZoneInfo(tz_name)
    cal = Calendar()
    cal.add("prodid", "-//Rehearsal Schedule Sync//animeisamistake.com//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)
    cal.add("x-wr-timezone", tz_name)

    for ev in events:
        try:
            start = datetime.strptime(
                f"{ev['date']} {ev['start_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=tz)
            end = datetime.strptime(
                f"{ev['date']} {ev['end_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=tz)
        except (KeyError, ValueError):
            continue  # skip malformed rows rather than crash the whole run

        # Stable UID so re-syncing the same event updates it instead of duplicating
        uid_seed = f"{ev['date']}|{ev['start_time']}|{ev.get('title','')}"
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}|{uid_seed}"))

        vevent = Event()
        vevent.add("uid", f"{uid}@animeisamistake.com")
        vevent.add("summary", ev.get("title", "Rehearsal"))
        vevent.add("dtstart", start)
        vevent.add("dtend", end)
        vevent.add("dtstamp", datetime.now(dt_timezone.utc))
        if ev.get("location"):
            vevent.add("location", ev["location"])
        if ev.get("notes"):
            vevent.add("description", ev["notes"])
        cal.add_component(vevent)

    return cal.to_ical()


def main():
    doc_id = os.environ.get("GOOGLE_DOC_ID")
    api_key = os.environ.get("GROQ_API_KEY")
    tz_name = os.environ.get("TIMEZONE", "Australia/Adelaide")
    cal_name = os.environ.get("CALENDAR_NAME", "Rehearsal Schedule")
    cutoff_str = os.environ.get("CUTOFF_DATE", "2026-12-31")

    if not doc_id or not api_key:
        print("Missing GOOGLE_DOC_ID or GROQ_API_KEY", file=sys.stderr)
        sys.exit(1)

    cutoff = datetime.strptime(cutoff_str, "%Y-%m-%d").date()
    today_date = datetime.now(ZoneInfo(tz_name)).date()
    if today_date > cutoff:
        print(
            f"Today ({today_date}) is past the cutoff ({cutoff}). "
            "Sync is retired — doing nothing. "
            "(Delete/disable .github/workflows/update-calendar.yml to stop "
            "the workflow from even running.)"
        )
        return

    doc_text = trim_to_summary(fetch_doc_text(doc_id))
    new_hash = content_hash(doc_text)
    old_hash = load_last_hash()

    if new_hash == old_hash and os.path.exists(ICS_FILE):
        print("No changes detected. Skipping Groq call.")
        return

    print("Change detected (or first run) — parsing with Groq...")
    today = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    events = call_groq(doc_text, api_key, today)
    print(f"Parsed {len(events)} events.")

    ics_bytes = build_ics(events, tz_name, cal_name, doc_id)
    os.makedirs(os.path.dirname(ICS_FILE), exist_ok=True)
    with open(ICS_FILE, "wb") as f:
        f.write(ics_bytes)

    save_hash(new_hash)
    print("Calendar updated.")


if __name__ == "__main__":
    main()
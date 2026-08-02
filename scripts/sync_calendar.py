#!/usr/bin/env python3
"""
Fetch a public Google Doc, detect changes since last run, parse the rehearsal
schedule with Groq in chunks, and (re)build an ICS calendar file.

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
import re
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
GROQ_MODEL = "openai/gpt-oss-120b"

# Chunking settings to strictly respect Groq's free tier rate limits
CHUNK_MAX_CHARS = 3500
CHUNK_DELAY_SECONDS = 20  # Spacing between chunk calls to stay under TPM/RPM
MAX_RETRIES = 5           # Retries per chunk on 429 rate-limit responses

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
- IMPORTANT: You may be shown a fragment of a larger document. Only extract \
events that are clearly stated in this fragment. If a line or table entry is cut off, \
ignore it.
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


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Splits full text into chunks along line boundaries."""
    lines = text.splitlines(keepends=True)
    chunks = []
    current_chunk = []
    current_len = 0

    for line in lines:
        if current_len + len(line) > max_chars and current_chunk:
            chunks.append("".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line)

    if current_chunk:
        chunks.append("".join(current_chunk))

    return chunks


def _parse_retry_wait(resp: requests.Response, default: float = 15.0) -> float:
    """Pull the 'try again in Xs' hint out of a Groq 429 body, else fall back."""
    try:
        msg = resp.json()["error"]["message"]
        m = re.search(r"try again in ([\d.]+)s", msg)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return default


def call_groq_single_chunk(
    chunk_text: str, api_key: str, today: str, max_retries: int = MAX_RETRIES
) -> list:
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_completion_tokens": 1024,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Today's date is {today}.\n\nDoc text fragment:\n{chunk_text}",
            },
        ],
    }

    resp = None
    for attempt in range(1, max_retries + 1):
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )

        if resp.status_code == 429:
            wait = _parse_retry_wait(resp) + 2  # small buffer on top of Groq's estimate
            print(
                f"    Rate limited (attempt {attempt}/{max_retries}), "
                f"waiting {wait:.1f}s before retry..."
            )
            time.sleep(wait)
            continue

        if not resp.ok:
            raise RuntimeError(
                f"Groq API error {resp.status_code}: {resp.text[:1000]}"
            )

        break
    else:
        raise RuntimeError(
            f"Exceeded {max_retries} retries due to persistent Groq rate limiting"
        )

    raw = resp.json()["choices"][0]["message"]["content"].strip()

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


def call_groq_full_doc(full_text: str, api_key: str, today: str) -> list:
    chunks = chunk_text(full_text)
    total_chunks = len(chunks)
    all_events = []

    print(f"Processing full document across {total_chunks} chunk(s)...")

    for i, chunk in enumerate(chunks, 1):
        print(f" -> Processing chunk {i}/{total_chunks}...")
        events = call_groq_single_chunk(chunk, api_key, today)
        all_events.extend(events)

        if i < total_chunks:
            print(f"    Waiting {CHUNK_DELAY_SECONDS}s to respect Groq rate limits...")
            time.sleep(CHUNK_DELAY_SECONDS)

    return all_events


def build_ics(events: list, tz_name: str, cal_name: str, doc_id: str) -> bytes:
    tz = ZoneInfo(tz_name)
    cal = Calendar()
    cal.add("prodid", "-//Rehearsal Schedule Sync//animeisamistake.com//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)
    cal.add("x-wr-timezone", tz_name)

    seen_signatures = set()

    for ev in events:
        try:
            start = datetime.strptime(
                f"{ev['date']} {ev['start_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=tz)
            end = datetime.strptime(
                f"{ev['date']} {ev['end_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=tz)
        except (KeyError, ValueError):
            continue

        # Prevent duplicate events from chunk boundary overlaps
        dedup_key = f"{ev['date']}|{ev['start_time']}|{ev['end_time']}|{ev.get('title','')}"
        if dedup_key in seen_signatures:
            continue
        seen_signatures.add(dedup_key)

        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}|{dedup_key}"))

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
            "Sync is retired — doing nothing."
        )
        return

    full_doc_text = fetch_doc_text(doc_id)
    new_hash = content_hash(full_doc_text)
    old_hash = load_last_hash()

    if new_hash == old_hash and os.path.exists(ICS_FILE):
        print("No changes detected. Skipping Groq call.")
        return

    print("Change detected — parsing full Google Doc with Groq...")
    today = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    events = call_groq_full_doc(full_doc_text, api_key, today)
    print(f"Parsed {len(events)} total unique event(s).")

    ics_bytes = build_ics(events, tz_name, cal_name, doc_id)
    os.makedirs(os.path.dirname(ICS_FILE), exist_ok=True)
    with open(ICS_FILE, "wb") as f:
        f.write(ics_bytes)

    save_hash(new_hash)
    print("Calendar successfully updated.")


if __name__ == "__main__":
    main()
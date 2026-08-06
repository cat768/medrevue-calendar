#!/usr/bin/env python3
"""
Fetch a public Google Doc, detect changes since last run, parse the rehearsal
schedule with Groq in chunks, merge any duplicate write-ups of the same
session, resolve locations into something you could actually navigate to,
and (re)build an ICS calendar file.

Location handling is deliberately two-tier and cached to disk:
  1. Cheap check against OpenStreetMap's free Nominatim geocoder -- if the
     raw text already resolves to somewhere sensible near Adelaide, leave it
     alone.
  2. Otherwise, ask a Groq "compound-mini" model (same free API key, built-in
     web search) to look it up against Adelaide University's current site and
     return a best-effort, honestly-confidence-rated resolution. Low
     confidence is never turned into a confident-sounding guess.
Results are cached in docs/.location_cache.json so repeat hourly runs barely
touch either API.

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
from tqdm import tqdm

STATE_FILE = "docs/.last_hash"
ICS_FILE = "docs/calendar.ics"
LOCATION_CACHE_FILE = "docs/.location_cache.json"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

# Compound systems bolt built-in web search onto an underlying Groq model.
# Only used for the small number of unique, ambiguous *locations* per run
# (not the main schedule extraction) -- like GROQ_MODEL, Groq occasionally
# deprecates/renames these; check console.groq.com/docs/compound if this
# starts erroring. A failure here is non-fatal: resolve_location() falls
# back to the raw text rather than aborting the whole sync.
GROQ_LOCATION_MODEL = "groq/compound-mini"

# Chunking settings to strictly respect Groq's free tier rate limits
CHUNK_MAX_CHARS = 3500
CHUNK_DELAY_SECONDS = 20   # Spacing between chunk calls to stay under TPM/RPM
MAX_RETRIES = 5            # Retries per chunk on 429 rate-limit responses
MAX_COMPLETION_TOKENS = 4096  # Generous headroom: gpt-oss-120b spends part of this
                               # budget on hidden reasoning tokens before it ever
                               # writes the JSON, even at reasoning_effort "low".
MAX_SPLIT_DEPTH = 2        # How many times a chunk may be halved if truncated
MIN_SPLIT_CHARS = 600      # Don't try to split below this size

# Location resolution settings
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "medrevue-calendar-sync/1.0 (github.com/cat768/medrevue-calendar)"
NOMINATIM_DELAY_SECONDS = 1.1     # Nominatim's usage policy caps free use at 1 req/sec
LOCATION_LLM_DELAY_SECONDS = 2.0  # Courtesy gap between compound-mini lookups
# Rough bounding box around the North Terrace campus, used only to *bias*
# (not restrict -- bounded=0) Nominatim towards campus buildings when a name
# is ambiguous.
ADELAIDE_UNI_VIEWBOX = "138.598,-34.913,138.613,-34.928"

SYSTEM_PROMPT = """You extract rehearsal schedule events from raw text copied from a \
Google Doc. Output ONLY a JSON array, no prose, no markdown fences.

Each element must have exactly these keys:
  "date": "YYYY-MM-DD"
  "start_time": "HH:MM" (24-hour)
  "end_time": "HH:MM" (24-hour) - if not stated, estimate a sensible duration \
or reuse start_time + 2 hours
  "title": short string, e.g. "Full Cast Rehearsal" or "Act 2 Scene 3"
  "location": string, empty string if not given
  "notes": string, empty string if not given -- pull together whatever would \
actually help someone show up prepared: what's being worked on (specific \
songs/scenes/acts), who needs to be there if it's not the full cast, \
call/warm-up times if they differ from the main block, and caveats like \
"may be cancelled" or "TBC". A sentence or two is plenty -- don't pad it out, \
and don't include shoutouts, jokes, or baking rosters, that's not what people \
need from a calendar reminder.

Rules:
- If the year is not stated, assume the nearest upcoming occurrence of that \
month/day relative to today.
- Skip rows that are clearly headers, not actual scheduled sessions.
- If you cannot find any valid events, output an empty JSON array: []
- Do not invent events that are not supported by the text.
- Copy "location" exactly as written in the source (nickname, room code, \
whatever it says). Don't try to expand abbreviations or guess a real address \
yourself -- a separate step handles turning it into something you could \
actually navigate to, and it needs your raw text unchanged to do that well.
- Some schedules describe the same session twice: once in a quick overview \
list, again later as a fuller write-up with its own time breakdown. If you \
can see both for the same date/time within this fragment, extract it once, \
using the fuller version. (If they land in different fragments you won't be \
able to tell -- that's fine, a later step merges same-timeslot duplicates \
across the whole document.)
- IMPORTANT: You may be shown a fragment of a larger document. Only extract \
events that are clearly stated in this fragment. If a line or table entry is cut off, \
ignore it.
- AM/PM: this is a REHEARSAL schedule -- sessions run in the afternoon/evening, \
never overnight. A breakdown table often states the meridiem once on the \
overall block (e.g. "Full Cast Rehearsal 5:30pm-8:00pm") and then lists \
sub-item rows with bare times ("5:45-6:15", "6:15-7:00", ...) that do NOT \
repeat "pm". Those bare sub-times inherit the same meridiem as the block \
they belong to. If you genuinely have no context to go on, treat any bare \
hour from 1-8 as PM, not AM -- do not output a start_time or end_time \
between 00:00 and 08:59 unless the source text explicitly says "am" or \
"morning".
"""

LOCATION_SYSTEM_PROMPT = """You help resolve short, informal venue names from a \
university theatre society's rehearsal schedule into a specific, real-world \
location description that would actually help someone find the place -- \
something concrete enough to plug into a maps app or to know which door to \
walk in.

The society rehearses at Adelaide University's North Terrace campus (Adelaide, \
South Australia) -- formed from the 2026 merger of the University of Adelaide \
and University of South Australia -- plus occasional off-campus venues. The \
raw text you're given is usually a short nickname, room code, or building \
abbreviation used casually by students, not an official address, and venues \
sometimes get renamed or relocated over time.

Use web search to check adelaide.edu.au (or wherever Adelaide University's \
current site/campus map lives now) when the raw text alone wouldn't be enough \
to find the place. Prefer current, official sources over guessing. If the raw \
text is already a specific, unambiguous place (e.g. a full address, or a \
well-known standalone venue), you don't need to search -- just confirm it.

Respond with ONLY a JSON object, no prose, no markdown fences:
{
  "resolved": "<best specific, mappable location description, or null>",
  "confidence": "high" | "medium" | "low"
}

Being vague-but-correct beats being precise-but-wrong: if you can't find a \
confident, current answer, set "resolved" to null (or use "low" confidence) \
rather than inventing a specific gate number, room code, or address you're \
not sure about."""


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
    chunk_text: str,
    api_key: str,
    today: str,
    max_retries: int = MAX_RETRIES,
    _depth: int = 0,
) -> list:
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
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
            tqdm.write(
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

    choice = resp.json()["choices"][0]
    finish_reason = choice.get("finish_reason")
    raw = choice["message"]["content"].strip()

    # gpt-oss models burn part of max_completion_tokens on hidden reasoning
    # before ever writing the JSON. If we got cut off mid-output, don't fail
    # the whole run — split the fragment in half and let each half finish
    # comfortably within budget.
    if finish_reason == "length":
        if _depth >= MAX_SPLIT_DEPTH or len(chunk_text) < MIN_SPLIT_CHARS:
            raise RuntimeError(
                "Groq output was truncated (finish_reason=length) and this "
                "fragment is already too small to split further. Consider "
                "raising MAX_COMPLETION_TOKENS."
            )
        tqdm.write(
            f"    Output truncated at depth {_depth}, splitting fragment "
            f"({len(chunk_text)} chars) in half and retrying..."
        )
        lines = chunk_text.splitlines(keepends=True)
        mid = max(1, len(lines) // 2)
        first_half, second_half = "".join(lines[:mid]), "".join(lines[mid:])

        events = call_groq_single_chunk(
            first_half, api_key, today, max_retries, _depth + 1
        )
        time.sleep(CHUNK_DELAY_SECONDS)
        events += call_groq_single_chunk(
            second_half, api_key, today, max_retries, _depth + 1
        )
        return events

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

    with tqdm(total=total_chunks, desc="Parsing rehearsal schedule", unit="chunk") as bar:
        for i, chunk in enumerate(chunks, 1):
            events = call_groq_single_chunk(chunk, api_key, today)
            all_events.extend(events)
            bar.update(1)

            if i < total_chunks:
                bar.set_postfix_str(f"cooling down {CHUNK_DELAY_SECONDS}s (rate limit)")
                time.sleep(CHUNK_DELAY_SECONDS)
                bar.set_postfix_str("")

    return all_events


def load_location_cache() -> dict:
    if os.path.exists(LOCATION_CACHE_FILE):
        try:
            with open(LOCATION_CACHE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_location_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(LOCATION_CACHE_FILE), exist_ok=True)
    with open(LOCATION_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def looks_directly_mappable(raw_location: str, session: requests.Session) -> bool:
    """Quick, free check via OpenStreetMap Nominatim: does this string, as
    written, already resolve to somewhere near Adelaide? If so there's no
    need to go figure out what it 'really' means -- a maps app already will."""
    try:
        resp = session.get(
            NOMINATIM_URL,
            params={
                "q": raw_location,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "au",
                "viewbox": ADELAIDE_UNI_VIEWBOX,
                "bounded": 0,
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=15,
        )
        if not resp.ok:
            return False
        results = resp.json()
        if not results:
            return False
        display = results[0].get("display_name", "")
        return "South Australia" in display or "Adelaide" in display
    except (requests.RequestException, ValueError):
        return False


def resolve_via_groq_search(raw_location: str, api_key: str) -> dict | None:
    """Ask a web-search-capable Groq model to resolve an ambiguous location.
    Returns a dict like {"resolved": ..., "confidence": ...} or None if the
    call failed/was unusable -- this is a best-effort enhancement, never
    allowed to break the sync."""
    payload = {
        "model": GROQ_LOCATION_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": LOCATION_SYSTEM_PROMPT},
            {"role": "user", "content": f'Raw location text: "{raw_location}"'},
        ],
    }
    try:
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
            tqdm.write(
                f"    Location lookup for \"{raw_location}\" failed "
                f"(HTTP {resp.status_code}), leaving it unresolved."
            )
            return None

        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
        tqdm.write(f"    Location lookup for \"{raw_location}\" errored ({e}), leaving it unresolved.")
        return None


def resolve_location(raw_location: str, cache: dict, api_key: str, session: requests.Session) -> None:
    """Populate cache[raw_location.lower()] with {"location": ..., "note": ...}.
    Keeps already-mappable text untouched; only reaches for the LLM+web-search
    fallback when the raw text alone wouldn't get someone there, and never
    invents specifics it isn't confident about."""
    text = raw_location.strip()
    key = text.lower()
    if not text or key in cache:
        return

    # Med students already know where this one is -- pass it through as-is,
    # no need to burn a Nominatim/Groq lookup on it.
    if key == "ahms 4050a/b":
        cache[key] = {"location": text, "note": None}
        return

    if looks_directly_mappable(text, session):
        time.sleep(NOMINATIM_DELAY_SECONDS)
        cache[key] = {"location": text, "note": None}
        return
    time.sleep(NOMINATIM_DELAY_SECONDS)

    resolution = resolve_via_groq_search(text, api_key) if api_key else None
    time.sleep(LOCATION_LLM_DELAY_SECONDS)

    resolved = (resolution or {}).get("resolved")
    confidence = (resolution or {}).get("confidence")
    if resolved and confidence in ("high", "medium"):
        cache[key] = {
            "location": f'{resolved.strip()} (listed as "{text}")',
            "note": None,
        }
        return

    # Couldn't confidently resolve it -- don't invent details. Keep the raw
    # text (at least pointed at the right campus/city so it's *somewhat*
    # mappable) and flag it so a human can fill in the gap.
    cache[key] = {
        "location": f"{text}, Adelaide University, Adelaide SA",
        "note": f'Exact location for "{text}" could not be confirmed automatically -- check with the committee.',
    }


def resolve_event_locations(events: list, api_key: str) -> list:
    """Resolve every unique raw location across the parsed events (cheap
    geocoding first, web-search-enabled LLM fallback second), caching results
    to disk so repeat hourly runs barely touch the network. Mutates and
    returns `events` with resolved locations and any resolution caveats
    folded into notes."""
    cache = load_location_cache()
    session = requests.Session()

    unique_raw = sorted({
        (ev.get("location") or "").strip()
        for ev in events
        if (ev.get("location") or "").strip()
    })

    if unique_raw:
        print(f"Resolving {len(unique_raw)} unique location(s)...")
        for raw in tqdm(unique_raw, desc="Resolving locations", unit="loc"):
            resolve_location(raw, cache, api_key, session)

    # Always (re)write the cache file once we get this far, even if there
    # was nothing new to resolve -- keeps `git add docs/.location_cache.json`
    # in the workflow from failing on a file that doesn't exist yet.
    save_location_cache(cache)

    for ev in events:
        raw = (ev.get("location") or "").strip()
        if not raw:
            continue
        cached = cache.get(raw.lower())
        if not cached:
            continue
        ev["location"] = cached["location"]
        if cached.get("note"):
            ev["notes"] = f"{ev['notes']}\n\n{cached['note']}" if ev.get("notes") else cached["note"]

    return events


EARLIEST_PLAUSIBLE_HOUR = 1   # inclusive
LATEST_IMPLAUSIBLE_HOUR = 8   # inclusive -- anything in [1,8] gets +12h


def fix_ambiguous_am_pm(events: list) -> list:
    """Second line of defense, independent of the prompt fix above. This is
    a rehearsal schedule for a uni theatre society -- sessions are never
    scheduled between midnight and 9am. If Groq still emits a start/end
    time in that window (e.g. it read a bare "5:45" sub-item as AM despite
    the block around it being stated in pm), shift it 12 hours forward.

    Deliberately excludes hour 0 (00:xx) -- a literal midnight boundary is
    ambiguous enough (genuine late finish vs. misparse) that guessing wrong
    silently is worse than leaving it alone.

    If the group ever schedules a genuine early-morning call (e.g. an 8am
    tech load-in), this will mangle it -- narrow the range below or drop
    the affected date from correction manually.
    """

    def _shift(time_str: str | None) -> str | None:
        if not time_str:
            return time_str
        try:
            hh, mm = time_str.split(":")
            hh = int(hh)
        except (ValueError, AttributeError):
            return time_str
        if EARLIEST_PLAUSIBLE_HOUR <= hh <= LATEST_IMPLAUSIBLE_HOUR:
            return f"{hh + 12:02d}:{mm}"
        return time_str

    for ev in events:
        ev["start_time"] = _shift(ev.get("start_time"))
        ev["end_time"] = _shift(ev.get("end_time"))
    return events


def _slot_richness(ev: dict) -> tuple:
    """How much did we actually learn about this event? Used to pick a
    winner when the same time slot shows up more than once in the source doc
    (e.g. a quick overview list up top, then a fuller write-up further down)."""
    title = (ev.get("title") or "").strip()
    is_generic_title = title.lower() in ("", "rehearsal", "rehearsal.")
    return (
        0 if is_generic_title else 1,
        1 if (ev.get("notes") or "").strip() else 0,
        1 if (ev.get("location") or "").strip() else 0,
        len(title),
    )


def dedupe_same_time_slots(events: list) -> list:
    """Some rehearsal-schedule docs describe the same session twice -- once
    in a terse overview line, again in a fuller per-rehearsal write-up.
    Both extractions are individually "correct", they're just two views of
    one session, so keep only the richer one per (date, start, end) slot
    instead of double-booking it."""
    best = {}
    order = []
    for ev in events:
        try:
            slot = (ev["date"], ev["start_time"], ev["end_time"])
        except KeyError:
            continue
        if slot not in best:
            order.append(slot)
            best[slot] = ev
        elif _slot_richness(ev) > _slot_richness(best[slot]):
            best[slot] = ev
    return [best[slot] for slot in order]


def group_events_by_date(events: list) -> dict:
    """Groups individual rehearsal entries by date, sorting each day's
    entries by start time. Entries missing a usable date/start/end are
    dropped, same as the old per-event build_ics silently skipped them."""
    by_date: dict = {}
    for ev in events:
        if not ev.get("date") or not ev.get("start_time") or not ev.get("end_time"):
            continue
        by_date.setdefault(ev["date"], []).append(ev)

    for day_events in by_date.values():
        day_events.sort(key=lambda e: e["start_time"])

    return by_date


def _day_summary(day_events: list) -> str:
    """One-line summary for the day's single calendar event. A single
    session just uses its own title; multiple sessions get combined,
    falling back to a plain count if that combination would be unwieldy."""
    titles = [(ev.get("title") or "Rehearsal").strip() for ev in day_events]
    if len(titles) == 1:
        return titles[0]
    joined = " / ".join(titles)
    return joined if len(joined) <= 80 else f"{len(titles)} Rehearsals"


def _day_location(day_events: list) -> str:
    """Unique locations across the day's sessions, in order of appearance."""
    locations = []
    for ev in day_events:
        loc = (ev.get("location") or "").strip()
        if loc and loc not in locations:
            locations.append(loc)
    return "; ".join(locations)


def _day_description(day_events: list) -> str:
    """Folds every session on the day into the single event's notes, one
    block per session: time range, title, location, then any notes."""
    blocks = []
    for ev in day_events:
        title = (ev.get("title") or "Rehearsal").strip()
        time_range = f"{ev.get('start_time', '?')}\u2013{ev.get('end_time', '?')}"
        block_lines = [f"{time_range} \u2014 {title}"]

        location = (ev.get("location") or "").strip()
        if location:
            block_lines.append(f"Location: {location}")

        notes = (ev.get("notes") or "").strip()
        if notes:
            block_lines.append(notes)

        blocks.append("\n".join(block_lines))

    return "\n\n".join(blocks)


def build_ics(events: list, tz_name: str, cal_name: str, doc_id: str) -> bytes:
    tz = ZoneInfo(tz_name)
    cal = Calendar()
    cal.add("prodid", "-//Rehearsal Schedule Sync//animeisamistake.com//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)
    cal.add("x-wr-timezone", tz_name)

    by_date = group_events_by_date(events)

    for date in sorted(by_date):
        day_events = by_date[date]

        try:
            starts = [
                datetime.strptime(
                    f"{date} {ev['start_time']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=tz)
                for ev in day_events
            ]
            ends = [
                datetime.strptime(
                    f"{date} {ev['end_time']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=tz)
                for ev in day_events
            ]
        except ValueError:
            continue

        start = min(starts)
        end = max(ends)

        # UID is keyed on the date only (not on content) so the same day
        # keeps the same event across reruns -- calendar apps update it in
        # place instead of duplicating it every time the doc changes.
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}|{date}"))

        vevent = Event()
        vevent.add("uid", f"{uid}@animeisamistake.com")
        vevent.add("summary", _day_summary(day_events))
        vevent.add("dtstart", start)
        vevent.add("dtend", end)
        vevent.add("dtstamp", datetime.now(dt_timezone.utc))

        location = _day_location(day_events)
        if location:
            vevent.add("location", location)

        vevent.add("description", _day_description(day_events))

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
    print(f"Parsed {len(events)} raw event(s).")

    events = fix_ambiguous_am_pm(events)

    events = dedupe_same_time_slots(events)
    print(f"{len(events)} event(s) after merging same-timeslot duplicates.")

    events = resolve_event_locations(events, api_key)

    ics_bytes = build_ics(events, tz_name, cal_name, doc_id)
    os.makedirs(os.path.dirname(ICS_FILE), exist_ok=True)
    with open(ICS_FILE, "wb") as f:
        f.write(ics_bytes)

    save_hash(new_hash)
    print("Calendar successfully updated.")


if __name__ == "__main__":
    main()
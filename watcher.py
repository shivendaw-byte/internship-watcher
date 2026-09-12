#!/usr/bin/env python3
"""Internship application watcher.

Polls each configured careers site, works out which postings are genuinely new
since the last run, filters them down to sophomore-relevant summer internships,
and emails a digest.

Usage:
    python watcher.py                  # normal run (emails only if there's news)
    python watcher.py --dry-run        # print to console, send nothing, save nothing
    python watcher.py --notify-first-run   # email everything currently open too
    python watcher.py --test-email     # verify SMTP credentials work
    python watcher.py --only Visa      # run a single source (repeatable)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

import requests
import yaml

from dataclasses import dataclass

import export
import notify
import sources
from sources import ADAPTERS, Job, SourceError

ROOT = pathlib.Path(__file__).resolve().parent


@dataclass(frozen=True)
class Listing:
    """A posting plus everything the digest and the sheet need to show."""

    job: Job
    verdict: str                # match | review
    priority: bool
    function: str               # econ_policy | business | consulting_adjacent | ...
    reason: str                 # why it landed on that verdict
    source_type: str            # workday, github_markdown, ...
    fingerprint: str            # cross-source identity

# Job titles contain em-dashes and emoji; the default Windows console codepage
# (cp1252) raises on them, which would crash a run that otherwise succeeded.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:  # local convenience; on GitHub Actions the secrets are already in env
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("! state.json was corrupt; starting fresh", file=sys.stderr)
    return {"sources": {}, "last_email": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------

_KW_CACHE: dict[str, "re.Pattern[str]"] = {}

# Workday returns placeholders like "3 Locations" instead of a real place.
_VAGUE_LOCATION = re.compile(r"^\d+\s+locations?$", re.I)

# A US marker beats the country/city blocklist. Without this, "New London, CT"
# is dropped because it contains "London", and "Ontario, CA" because it
# contains "Ontario" -- both are real US cities.
_US_ABBR = re.compile(
    r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|"
    r"VA|WA|WV|WI|WY|DC)\b"
)
_US_WORD = re.compile(r"\b(?:U\.?S\.?A?|United States)\b", re.I)
_US_STATE_NAMES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware "
    "florida georgia hawaii idaho illinois indiana iowa kansas kentucky "
    "louisiana maine maryland massachusetts michigan minnesota mississippi "
    "missouri montana nebraska nevada ohio oklahoma oregon pennsylvania "
    "tennessee texas utah vermont virginia washington wisconsin wyoming"
).split()


def _looks_american(location: str) -> bool:
    """True when the location clearly names somewhere in the US."""
    if _US_WORD.search(location) or _US_ABBR.search(location):
        return True
    low = location.lower()
    return any(_kw(s).search(low) for s in _US_STATE_NAMES)


def _kw(word: str):
    """Whole-word keyword matcher.

    Plain substring matching is wrong here: "International Corporate Tax
    Advisory - Manager" contains "intern" and was being emailed as an
    internship. Allow a trailing plural 's' but nothing else.
    """
    rx = _KW_CACHE.get(word)
    if rx is None:
        rx = re.compile(r"(?<![a-z])" + re.escape(word.strip().lower()) + r"s?(?![a-z])")
        _KW_CACHE[word] = rx
    return rx


def _location_ok(job: Job, rules: dict) -> bool:
    """Location filtering FAILS OPEN, on purpose.

    An allowlist of US cities silently dropped real roles in McLean, Santa
    Clara, Plano and anywhere else nobody thought to list. So instead: keep
    everything except postings whose location clearly names somewhere he
    can't work. Whole-word matching matters here -- "India" must not match
    "Indianapolis, Indiana".
    """
    loc = job.location.lower()
    if not loc or _VAGUE_LOCATION.match(job.location) or _looks_american(job.location):
        return True
    if any(_kw(s).search(loc) for s in rules.get("locations_exclude") or []):
        return False
    locs = [s.lower() for s in rules.get("locations_any") or []]
    if locs and not any(s in loc for s in locs):
        return False
    return True


def detect_function(job: Job, rules: dict) -> str | None:
    """Which lane a posting sits in, or None when nothing matches."""
    hay = f"{job.title} {job.location}".lower()
    for bucket, terms in (rules.get("functions") or {}).items():
        if any(_kw(t).search(hay) for t in terms):
            return bucket
    return None


# Verdicts. "review" exists so an ambiguous posting reaches a human instead of
# being discarded on a guess -- missing one real match costs far more than
# reading one extra line in a digest.
MATCH, REVIEW, REJECT = "match", "review", "reject"


def classify(job: Job, rules: dict) -> tuple[str, bool, str | None, str]:
    """Return (verdict, is_priority, function, reason).

    Staged so the reason is always specific enough to act on:
      1. not an early-career role            -> reject (silent, high volume)
      2. needs a degree/experience he lacks  -> reject
      2b. aimed at juniors/seniors only      -> reject
      3. MBB management consulting           -> reject (out of scope by choice)
      4. location he can't work in           -> reject
      5. has an underclassman/2027 signal    -> match
      6. anything else                       -> review, with the gap named
    """
    hay = f"{job.title} {job.location}".lower()

    role_terms = rules.get("role_any") or []
    if role_terms and not any(_kw(s).search(hay) for s in role_terms):
        return REJECT, False, None, "not an early-career role"

    for term in rules.get("hard_exclude") or []:
        if _kw(term).search(hay):
            return REJECT, False, None, f"requires {term}"

    for term in rules.get("senior_only") or []:
        if _kw(term).search(hay):
            return REJECT, False, None, f"aimed at {term}"

    # Checked before the function buckets so "management consulting" rejects
    # even though the bare word "consulting" maps to consulting_adjacent.
    for term in rules.get("function_exclude") or []:
        if _kw(term).search(hay):
            return REJECT, False, None, f"traditional MBB consulting ({term})"

    if not _location_ok(job, rules):
        return REJECT, False, None, f"location out of scope ({job.location})"

    function = detect_function(job, rules)
    priority = any(_kw(s).search(hay) for s in rules.get("priority_any") or [])
    has_class_signal = any(
        _kw(s).search(hay) for s in rules.get("class_signals") or [])

    if has_class_signal and function:
        return MATCH, priority, function, "underclassman signal + known function"
    if has_class_signal and not function:
        return REVIEW, priority, None, "eligible year, but function unclear"
    if function:
        return REVIEW, priority, function, "no class-year stated - check eligibility"
    return REVIEW, priority, None, "no class-year stated and function unclear"


# ---------------------------------------------------------------------------
# cross-source deduplication
# ---------------------------------------------------------------------------

_FP_SPLIT = re.compile(r"\s+[-–—]\s+")

# Sources that scrape an employer directly. When the same posting arrives from
# both a curated list and the employer's own board, the direct one wins -- it
# is closer to the origin and carries the real apply URL.
DIRECT_TYPES = {"workday", "successfactors", "google_careers", "amazon_jobs",
                "usajobs", "greenhouse", "lever"}


def fingerprint(job: Job, src_type: str) -> str:
    """A source-independent identity for a posting.

    The per-source `seen` sets can't catch a posting that appears in both a
    GitHub list and an employer's Workday board -- those have different ids,
    so it was previously counted and emailed twice. This collapses them on
    normalised company + role text.
    """
    title = job.title
    parts = _FP_SPLIT.split(title, 1)
    if len(parts) == 2 and src_type not in DIRECT_TYPES:
        company, role = parts[0], parts[1]
    else:
        company, role = job.source, title
    role = re.sub(r"\(.*?\)|\[.*?\]", " ", role)          # drop parentheticals
    role = re.sub(r"\b(20\d\d|summer|fall|winter|spring)\b", " ", role, flags=re.I)
    blob = re.sub(r"[^a-z0-9]+", "", (company + role).lower())
    return blob[:70]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(args) -> int:
    cfg = load_config()
    state = load_state()
    rules = cfg.get("match", {})
    email_cfg = cfg.get("email", {})

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    new_jobs: list[Listing] = []
    all_listings: list[Listing] = []
    health: list[str] = []
    stats: dict[str, str] = {}

    for src in cfg.get("sources", []):
        name = src["name"]
        if src.get("enabled") is False:
            continue
        if args.only and name not in args.only:
            continue

        adapter = ADAPTERS.get(src.get("type"))
        if adapter is None:
            health.append(f"{name}: unknown source type {src.get('type')!r}")
            continue

        prev = state["sources"].get(name, {})
        seen: set[str] = set(prev.get("seen", []))
        first_run = not prev

        try:
            jobs = adapter(src, session)
        except SourceError as exc:
            health.append(f"{name}: {exc}")
            stats[name] = "FAILED"
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # adapter bug -- still must be loud
            health.append(f"{name}: unexpected {type(exc).__name__}: {exc}")
            stats[name] = "FAILED"
            print(f"[{name}] UNEXPECTED: {exc}", file=sys.stderr)
            continue

        # Non-fatal problems the adapter noticed (e.g. table columns it did not
        # recognise, meaning rows were skipped). Surfaced in the email so silent
        # data loss becomes visible.
        for msg in sources.WARNINGS.pop(name, []):
            health.append(f"{name}: {msg}")

        # Per-source drop list, applied even when `filter: false`. Curated repos
        # mix in things you don't want (scholarships, tuition grants) that no
        # generic internship rule would catch.
        drop_terms = [t.lower() for t in src.get("exclude_terms") or []]
        if drop_terms:
            before = len(jobs)
            jobs = [
                j for j in jobs
                if not any(_kw(t).search(f"{j.title} {j.location}".lower())
                           for t in drop_terms)
            ]
            if before != len(jobs):
                print(f"[{name}] dropped {before - len(jobs)} by exclude_terms")

        apply_filter = src.get("filter", src.get("type") != "page_watch")
        src_type = src.get("type", "")
        kept: list[Listing] = []
        for j in jobs:
            if apply_filter:
                verdict, pri, function, reason = classify(j, rules)
            else:
                # Curated underclassmen lists are already filtered for his
                # class year, so don't re-gate them -- but still label the
                # function so the sheet stays useful.
                verdict, pri, reason = MATCH, False, "curated underclassmen list"
                function = detect_function(j, rules)
            if verdict == REJECT:
                continue
            kept.append(Listing(
                job=j, verdict=verdict, priority=pri,
                function=function or "unclassified", reason=reason,
                source_type=src_type, fingerprint=fingerprint(j, src_type)))

        all_listings.extend(kept)
        fresh = [L for L in kept if L.job.id not in seen]

        # A source that used to return postings and now returns none is far
        # more likely to be broken than genuinely empty. Sources that can
        # legitimately be empty (e.g. status-gated ones) opt out.
        if not jobs and prev.get("last_count") and not src.get("may_be_empty"):
            health.append(
                f"{name}: returned 0 postings but returned "
                f"{prev['last_count']} last time - the site may have changed"
            )

        stats[name] = f"{len(jobs)} total, {len(kept)} relevant"

        if first_run and not args.notify_first_run:
            print(f"[{name}] first run: seeding {len(kept)} relevant postings "
                  "(not emailed; use --notify-first-run to see them)")
            for L in kept:
                flag = "*" if L.priority else ("?" if L.verdict == REVIEW else "-")
                print(f"    {flag} [{L.verdict}] {L.job.title} | {L.job.url}")
        else:
            new_jobs.extend(fresh)
            print(f"[{name}] {len(fresh)} new of {len(kept)} relevant")

        if not args.dry_run:
            state["sources"][name] = {
                "seen": sorted(seen | {j.id for j in jobs}),
                "last_count": len(jobs),
                "last_ok": now_iso(),
            }

    # ---- cross-source dedup ---------------------------------------------
    # Per-source `seen` sets cannot catch the same posting arriving from a
    # curated GitHub list AND the employer's own board: different ids, so it
    # was counted and emailed twice. Collapse on fingerprint, and let the
    # direct-from-employer copy win since it carries the real apply URL.
    seen_fp: set[str] = set(state.get("seen_fingerprints", []))
    by_fp: dict[str, Listing] = {}
    dupes_collapsed = 0
    for L in sorted(new_jobs, key=lambda L: L.source_type not in DIRECT_TYPES):
        if L.fingerprint in by_fp:
            dupes_collapsed += 1
            continue
        by_fp[L.fingerprint] = L

    already_reported = [fp for fp in by_fp if fp in seen_fp]
    new_jobs = [L for fp, L in by_fp.items() if fp not in seen_fp]
    if dupes_collapsed or already_reported:
        print(f"dedup: collapsed {dupes_collapsed} same-run duplicate(s), "
              f"suppressed {len(already_reported)} already reported via "
              "another source")

    # ---- write the CSV the Google Sheet reads ---------------------------
    if not args.dry_run and all_listings:
        today = dt.date.today().isoformat()
        # Dedup across sources here too, so the sheet gets one row per posting.
        sheet_rows = list({
            L.fingerprint: L
            for L in sorted(all_listings,
                            key=lambda L: L.source_type not in DIRECT_TYPES)
        }.values())
        added, total = export.write(sheet_rows, today)
        print(f"listings.csv: +{added} new, {total} rows total")

    if not args.dry_run:
        state["seen_fingerprints"] = sorted(
            seen_fp | {L.fingerprint for L in new_jobs})

    # ---- decide whether to send -----------------------------------------
    heartbeat_days = int(email_cfg.get("heartbeat_days", 7))
    last_email = state.get("last_email")
    heartbeat_due = True
    if last_email:
        try:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_email)
            heartbeat_due = age.days >= heartbeat_days
        except ValueError:
            heartbeat_due = True

    should_send = bool(new_jobs) or bool(health) or heartbeat_due

    new_jobs.sort(key=lambda L: (L.verdict != MATCH, not L.priority,
                                L.job.source, L.job.title))
    subject, text_body, html_body = notify.render_digest(new_jobs, health, stats)
    subject = f"{email_cfg.get('subject_prefix', '[Internship Watch]')} {subject}"

    if args.dry_run:
        print("\n" + "=" * 60)
        print("SUBJECT:", subject)
        print("=" * 60)
        print(text_body)
        print("=" * 60)
        print(f"(dry run: would {'send' if should_send else 'NOT send'} this email; "
              "state not saved)")
        return 0

    if should_send:
        try:
            notify.send_email(subject, text_body, html_body)
            state["last_email"] = now_iso()
            print(f"Emailed: {subject}")
        except notify.NotifyError as exc:
            # Deliberately do NOT save state here. If the digest never went out,
            # these postings must stay "unseen" so the next run reports them
            # again -- otherwise a transient SMTP failure silently swallows the
            # one posting you were waiting for.
            print(f"! EMAIL FAILED (state not saved, will retry): {exc}",
                  file=sys.stderr)
            return 2
    else:
        print("Nothing new and no issues; no email sent.")

    save_state(state)
    return 1 if health else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the digest instead of emailing; do not save state")
    ap.add_argument("--notify-first-run", action="store_true",
                    help="email currently-open postings on a source's first run")
    ap.add_argument("--test-email", action="store_true",
                    help="send a test email to verify SMTP settings")
    ap.add_argument("--only", action="append", metavar="NAME",
                    help="only run this source (repeatable)")
    args = ap.parse_args()

    if args.test_email:
        try:
            notify.send_email(
                "[Internship Watch] Test email",
                "If you are reading this, SMTP is configured correctly.",
                "<p>If you are reading this, SMTP is configured correctly.</p>",
            )
        except notify.NotifyError as exc:
            print(f"! {exc}", file=sys.stderr)
            return 2
        print("Test email sent.")
        return 0

    return run(args)


if __name__ == "__main__":
    sys.exit(main())

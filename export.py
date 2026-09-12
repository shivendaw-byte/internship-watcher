"""Writes listings.csv -- the single source of truth for the Google Sheet.

The bot runs unattended in GitHub Actions, which has no Google credentials, so
it cannot call the Sheets API. Instead it commits a CSV to this public repo and
the Sheet pulls it with IMPORTDATA. Zero credentials, no Google Cloud project.

Consequence worth understanding: IMPORTDATA output is READ-ONLY in Sheets.
Anything you type into those cells is overwritten on the next refresh. That is
why `app_status` and `notes` live on a separate Tracker tab keyed by
listing_id, not in the imported range. See README.

Rows already in the CSV are preserved: `date_discovered` must not reset every
run, and a posting that disappears from a board still matters if you applied
to it. New runs merge in by listing_id.
"""

from __future__ import annotations

import csv
import pathlib

CSV_PATH = pathlib.Path(__file__).resolve().parent / "listings.csv"

COLUMNS = [
    "listing_id",         # fingerprint; stable across sources, joins to Tracker
    "company",
    "role_title",
    "function",           # econ_policy | business | consulting_adjacent | unclassified
    "eligibility",        # match | review
    "eligibility_reason", # why -- makes triaging "review" rows a few seconds
    "source_type",        # workday | github_markdown | usajobs | ...
    "source_name",        # which employer board or which list
    "location",
    "date_posted",        # only when the board actually publishes it
    "date_discovered",    # when this bot first saw it; never overwritten
    "deadline",
    "apply_url",
    "priority",           # TRUE when it names his class year / cycle
]


def _company_and_role(listing) -> tuple[str, str]:
    """Split "Company - Role" titles from curated lists; fall back to source."""
    import re

    title = listing.job.title
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(parts) == 2 and listing.source_type not in (
            "workday", "amazon_jobs", "google_careers", "usajobs",
            "successfactors"):
        return parts[0].strip(), parts[1].strip()
    return listing.job.source, title


def load_existing() -> dict[str, dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        return {r["listing_id"]: r for r in csv.DictReader(fh)
                if r.get("listing_id")}


def write(listings, today: str) -> tuple[int, int]:
    """Merge `listings` into listings.csv. Returns (added, total)."""
    rows = load_existing()
    added = 0

    for L in listings:
        lid = L.fingerprint
        if not lid:
            continue
        company, role = _company_and_role(L)
        if lid in rows:
            # Refresh the fields that can legitimately change, but never
            # date_discovered -- "how long has this been open" is the whole
            # point of that column.
            row = rows[lid]
            row["eligibility"] = L.verdict
            row["eligibility_reason"] = L.reason
            row["function"] = L.function
            row["apply_url"] = L.job.url or row.get("apply_url", "")
            row["priority"] = "TRUE" if L.priority else "FALSE"
            continue

        rows[lid] = {
            "listing_id": lid,
            "company": company,
            "role_title": role,
            "function": L.function,
            "eligibility": L.verdict,
            "eligibility_reason": L.reason,
            "source_type": L.source_type,
            "source_name": L.job.source,
            "location": L.job.location,
            "date_posted": L.job.posted,
            "date_discovered": today,
            "deadline": "",
            "apply_url": L.job.url,
            "priority": "TRUE" if L.priority else "FALSE",
        }
        added += 1

    # Newest discoveries first, and priority above the rest within a day, so
    # the top of the Sheet is the part worth reading.
    ordered = sorted(
        rows.values(),
        key=lambda r: (r.get("date_discovered", ""), r.get("priority") == "TRUE"),
        reverse=True,
    )
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)

    return added, len(ordered)

#!/usr/bin/env python3
"""On-demand application prep for ONE listing from listings.csv.

This is deliberately NOT part of the watcher. The bot's job is discovery and
tracking; generating tailored material for every match would burn tokens on
roles you'd never apply to, and would bury the few you care about.

It writes a prompt pack to prep/<listing_id>/ and prints the path. It does not
call an LLM, send anything, or touch the sheet. You open the pack, paste the
prompt you want into Claude, and keep control of what gets written.

    python apply_prep.py --list                  # show recent matches + ids
    python apply_prep.py --id <listing_id>       # build the pack
    python apply_prep.py --search "t. rowe"      # find a listing by text

Add --fetch to also pull the live job description into the pack (best-effort;
many boards are JS-rendered and will come back thin -- it says so when that
happens rather than pretending).
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
CSV_PATH = ROOT / "listings.csv"
PREP_DIR = ROOT / "prep"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Edit this once; every generated prompt inherits it.
PROFILE = """\
Shiven Dawda - sophomore at the University of Pennsylvania (2026-27).
Majors: Mathematical Economics and Political Science.
Targeting Summer 2027 internships.
Lanes: business/corporate, economic policy (Fed, IMF, World Bank, Treasury,
think tanks), and econ-consulting/research (Oliver Wyman ICG, Cornerstone
Research). Not recruiting for traditional MBB management consulting.
Founded and presides over the Undergraduate Geography Club at Penn.
Built several working automation tools (this internship watcher among them).
"""


def load_rows() -> list[dict]:
    if not CSV_PATH.exists():
        sys.exit(f"No {CSV_PATH.name} yet. Let the watcher run once first.")
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cmd_list(rows, limit=25):
    rows = [r for r in rows if r.get("eligibility") == "match"][:limit]
    if not rows:
        print("No 'match' rows yet.")
        return
    print(f"{'listing_id':34} {'company':22} role")
    print("-" * 96)
    for r in rows:
        print(f"{r['listing_id'][:32]:34} {r['company'][:20]:22} {r['role_title'][:40]}")
    print("\nBuild a pack with:  python apply_prep.py --id <listing_id>")


def cmd_search(rows, term):
    t = term.lower()
    hits = [r for r in rows
            if t in r["company"].lower() or t in r["role_title"].lower()]
    if not hits:
        print(f"Nothing matching {term!r}.")
        return
    for r in hits[:20]:
        print(f"{r['listing_id'][:32]:34} {r['company'][:20]:22} {r['role_title'][:42]}")


def fetch_jd(url: str) -> str:
    try:
        import requests
    except ImportError:
        return "(requests not installed)"
    try:
        r = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if r.status_code != 200:
            return f"(fetch returned HTTP {r.status_code})"
        body = re.sub(r"(?s)<(script|style).*?</\1>", " ", r.text)
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 400:
            return ("(page returned almost no text -- almost certainly "
                    "JavaScript-rendered. Open the URL and paste the "
                    "description in manually.)")
        return text[:6000]
    except Exception as exc:
        return f"(fetch failed: {type(exc).__name__})"


PROMPTS = {
    "1-resume-tailoring.md": """\
# Resume tailoring

Tailor my resume for the role below. Work only from things I have actually
done -- if a bullet would need an accomplishment I haven't given you, say so
and ask instead of inventing one.

Give me:
1. Which 4-6 existing bullets to keep, reordered by relevance to this posting
2. Rewrites of those bullets using the posting's own language where honest
3. Anything on my resume worth cutting for this specific application
4. Any genuine gap against the posting, stated plainly

{context}
""",
    "2-cover-letter.md": """\
# Cover letter

Draft a cover letter for the role below. Constraints:
- Under 300 words, three or four short paragraphs
- Reference ONE concrete thing I built or led, not a list
- No "I hope this email finds you well", no "I am writing to express"
- Say specifically why this employer and this function, not why internships
- Sound like a sophomore who is genuinely interested, not like a template

If you need a detail about my background that I haven't provided, ask rather
than filling it in.

{context}
""",
    "3-recruiter-outreach.md": """\
# Cold outreach to a recruiter or alum

Draft a short outreach email about the role below.

Rules:
- Under 120 words
- Leave the To: line EMPTY. Do not guess an address or an email pattern.
- One specific reason I'm interested in this team, not generic enthusiasm
- One clear ask (a short call, or whether the role is open to sophomores)
- No flattery

Also tell me where I could plausibly verify a real contact for this employer.

{context}
""",
    "4-referral-dm.md": """\
# LinkedIn referral request

Write two versions of a LinkedIn DM asking about a referral for this role:
(a) to a Penn alum at the company, (b) to someone I have no connection to.

Under 90 words each. Ask for a conversation, not a referral outright -- asking
a stranger for a referral cold usually fails. Make the Penn connection do the
work in version (a).

{context}
""",
    "5-interview-prep.md": """\
# STAR interview prep

For the role below, give me:
1. The 6 behavioural questions most likely to come up, given this function
2. For each, which of my actual experiences best answers it -- and if you
   don't have enough detail from me, say what to supply
3. A STAR skeleton per question (Situation/Task/Action/Result), with the
   Result left blank where I need to fill in a real number
4. 3 questions worth asking them that show I understand the function

Do not write fictional results.

{context}
""",
}


def cmd_build(rows, listing_id, fetch):
    row = next((r for r in rows if r["listing_id"] == listing_id), None)
    if row is None:
        near = [r["listing_id"] for r in rows if r["listing_id"].startswith(listing_id[:12])]
        sys.exit(f"No listing {listing_id!r}." +
                 (f" Did you mean: {', '.join(near[:3])}" if near else
                  " Run --list to see ids."))

    context = "\n".join([
        "## The role",
        f"Company:   {row['company']}",
        f"Title:     {row['role_title']}",
        f"Function:  {row['function']}",
        f"Location:  {row['location'] or 'not stated'}",
        f"Posted:    {row['date_posted'] or 'not stated'}",
        f"Apply:     {row['apply_url']}",
        f"Source:    {row['source_name']} ({row['source_type']})",
        f"Eligibility: {row['eligibility']} - {row['eligibility_reason']}",
        "",
        "## About me",
        PROFILE,
    ])

    if fetch:
        context += "\n## Job description (auto-fetched, verify it)\n" + \
                   fetch_jd(row["apply_url"]) + "\n"

    slug = re.sub(r"[^a-z0-9]+", "-", row["company"].lower())[:24].strip("-")
    out = PREP_DIR / f"{slug}-{listing_id[:10]}"
    out.mkdir(parents=True, exist_ok=True)

    (out / "0-context.md").write_text(context, encoding="utf-8")
    for fname, template in PROMPTS.items():
        (out / fname).write_text(template.format(context=context), encoding="utf-8")

    print(f"Prep pack: {out}")
    print(f"  {row['company']} - {row['role_title']}")
    for f in sorted(out.iterdir()):
        print(f"    {f.name}")
    print("\nOpen any of the numbered files and paste it into Claude.")
    print("Nothing was sent, and the sheet was not modified.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show recent matches")
    ap.add_argument("--search", metavar="TEXT", help="find a listing")
    ap.add_argument("--id", metavar="LISTING_ID", help="build a pack for one listing")
    ap.add_argument("--fetch", action="store_true",
                    help="also try to pull the live job description")
    args = ap.parse_args()

    rows = load_rows()
    if args.list:
        cmd_list(rows)
    elif args.search:
        cmd_search(rows, args.search)
    elif args.id:
        cmd_build(rows, args.id, args.fetch)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Changelog

## 2026-09-12 — Retarget to econ/policy, three-way eligibility, Google Sheet

Context for whoever picks this up next: the brief for this session assumed the
bot only scraped GitHub internship-list repos and already had Google Sheets
access. **Neither was true.** The audit is worth reading before trusting any
description of this system, including this one.

### What it did before

- **17 sources, 12 of them already direct-from-employer**: 8 Workday tenants
  (Mastercard, Visa, Capital One, NVIDIA, Salesforce, Adobe, PayPal, Federal
  Reserve System), Amazon's JSON API, Google Careers HTML, EY SuccessFactors,
  and the keyed USAJOBS API. Only 4 were GitHub markdown lists, plus apmlist.
  The "scrape closer to the origin" principle was already the majority of the
  system — this session retargeted that layer rather than inventing it.
- **Binary filter.** `classify()` returned keep/drop. Ambiguous postings were
  silently discarded.
- **Per-source dedup only.** The key was `source::id`, so one posting arriving
  from both a GitHub list and an employer board counted and emailed twice.
- **Email digest** over SMTP, with a priority section, source-health warnings,
  and a 7-day heartbeat.
- **No Google anything.** Zero Sheets/Drive/OAuth references anywhere.
- Tech-leaning source list and a generic intern keyword filter.

### What changed

**Sources — retargeted, not expanded blindly.**
- Removed NVIDIA, Salesforce, Adobe, PayPal — overwhelmingly SWE/hardware and
  pure noise against an econ profile.
- Added **Vanguard, T. Rowe Price, BlackRock, IMF** (Workday; all four tenants
  verified live 2026-09-12 returning 444 / 135 / 321 / 11 postings).
- Kept every GitHub list, the email path, the schedule, and all credentials
  untouched.
- Treasury, CBO and other federal bodies are already covered by the existing
  USAJOBS source; the Federal Reserve source covers all 12 regional banks
  including Philadelphia.

**Eligibility filter — now three-way.** `classify()` returns
`(verdict, priority, function, reason)` where verdict is `match` / `review` /
`reject`. A posting that looks early-career but states no class year becomes
**`review` with the gap named**, never a silent drop. Rejects are staged so the
reason is specific: not-early-career, requires a degree he lacks, aimed at
juniors/seniors, MBB management consulting (out of scope by choice), or an
out-of-scope location. Postings are also bucketed into `econ_policy`,
`business`, or `consulting_adjacent`; matching no bucket yields `review`.

**Cross-source dedup.** New `fingerprint()` builds a source-independent
identity from normalised company + role, stripping parentheticals and
season/year tokens. Duplicates collapse within a run, the direct-from-employer
copy wins (closer to origin, real apply URL), and `state.seen_fingerprints`
stops a posting re-emailing later via a second source.

**Google Sheet via committed CSV.** `export.py` writes `listings.csv`, which
the workflow commits; the Sheet pulls it with `IMPORTDATA`. Chosen over the
Sheets API deliberately — the bot runs in GitHub Actions with no Google
credentials, and a service account would have meant a Google Cloud project.
`date_discovered` is never overwritten, and rows persist after a posting
disappears from a board.

**Digest.** Same transport and formatting conventions, now with three sections
(priority / clear matches / needs review), plus source and function on every
line and the flag reason on review rows.

**`apply_prep.py`** — on-demand only. Builds a five-prompt pack (resume,
cover letter, recruiter outreach, referral DM, STAR prep) for one listing.
Zero references from `watcher.py` or the workflow, by design.

### Deliberately not changed

- The GitHub-list scraping, the SMTP path, the cron schedule, and every
  existing secret. Verified untouched.
- LinkedIn and Indeed: **skipped**, not worked around. Both prohibit scraping
  in their ToS. Greenhouse and Lever adapters already exist as the compliant
  fallback if a target employer uses them.
- Fidelity, World Bank, Brookings, Cornerstone Research, Oliver Wyman: **not
  added.** Fidelity and World Bank Workday tenant guesses returned HTTP 422;
  Brookings' iCIMS board 302-redirects; Cornerstone Research is on iCIMS
  (confirmed) but the tenant wasn't resolved; Oliver Wyman is Phenom and
  entirely JavaScript-rendered. Each needs a new adapter or a headless
  browser. Shipping a source that looks healthy while silently returning
  nothing is the worst outcome here, so they were left out.
- The `state.json` seen-sets were left intact, so changing the filter did not
  re-notify anything — previously-scanned ids stay scanned.

### Known gaps

- `BlackRock` returns 0 relevant of 321. Verified genuine: its "Analyst"
  postings are full-time job levels and the summer programmes aren't open yet.
  Not a bug, but re-check in October.
- The grad year is inconsistent in the brief ("sophomore" and "graduating
  2028" can't both hold — a 2026-27 sophomore graduates 2029). Built for
  sophomore standing and Summer 2027, which both readings agree on.
- `listings.csv` grows without bound. Fine for a year; revisit if it passes a
  few thousand rows.

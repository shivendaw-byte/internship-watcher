# Internship watcher

Polls company careers sites every few hours, works out which postings are new,
filters to sophomore-relevant summer internships, and emails you a digest.

Target: **Summer 2027** internships (you're a sophomore in 2026–27). Most of
these open between August 2026 and January 2027.

## What's verified working

**Company career pages** — authoritative, but slow to surface anything:

| Company | How it's read | Status |
|---|---|---|
| Mastercard | Workday JSON API (`mastercard.wd1`) | ✅ 1118 postings pulled |
| Visa | Workday JSON API (`visa.wd5`) | ✅ 774 postings pulled |
| EY / EY-Parthenon | SuccessFactors search HTML | ✅ 271 postings parsed |
| Google | careers HTML (job links are server-rendered) | ✅ 5 US intern roles found |

**Community-curated lists** — where a sophomore-eligible role usually appears
*first*, often days before it's worth checking a careers page:

| Source | How it's read | Status |
|---|---|---|
| Cruz-Lopez/underclassmen-opportunities | README markdown tables | ✅ 46 roles |
| LuisaE/opportunities | README markdown tables | ✅ 137 roles |
| zapplyjobs/underclassmen-internships | README markdown tables | ✅ 48 roles |
| vanshb03/Summer2027-Internships | README markdown tables | ✅ 172 roles |
| apmlist.com | Next.js embedded JSON | ✅ 3 currently open |

apmlist is the standout: it publishes an explicit `Open` / `Not Yet Open`
status per program, so the watcher catches the exact moment one flips open
rather than waiting for a new listing to appear. Right now 35 of its 39 PM
internships are "Not Yet Open" — those are the ones you want to hear about.

### Three sources I couldn't use

- **simplify.jobs/dashboard** — your logged-in dashboard. Scraping it needs your
  session cookie stored in a repo, and its listings largely mirror the GitHub
  repos above anyway. Keep using Simplify manually for autofill and tracking.
- **intern-list.com** — renders client-side from jobright.ai; the only public
  endpoint returns aggregate counts, not listings, and isn't underclassmen-curated.
- **interninsider.me** — listings are injected by JavaScript; the served HTML
  contains no job data.

## What today's run actually found

**Visa and Google have no US Summer 2027 internships posted yet**, and
Mastercard's only 2027 intern roles are in Latin America — that's the reason
this bot is worth running, not a bug.

But the curated lists show things that are **open right now**, including Apple's
Undergrad Software / ML internships (Fall–Summer 2027), Microsoft Explore,
LinkedIn First Play, Google STEP, and open PM internships at Microsoft, TikTok
and Appian. Worth acting on this week.

## Setup — run one command

Right-click **`setup.ps1`** → *Run with PowerShell*. Or paste this into a
terminal:

```bash
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

It asks you three questions and does the other seven steps itself: signs you
into GitHub, creates the private repo, pushes the code, stores your email
settings as encrypted GitHub secrets, sends you everything open today, and
switches on the schedule. It's safe to re-run — it skips whatever is done.

**The one thing you must do by hand** is create a Gmail **App Password** (a
16-character code, not your normal password). The script tells you when and
links you straight there, but the steps are:

1. Turn on 2-Step Verification on the Google account
2. Go to <https://myaccount.google.com/apppasswords>
3. Create one named "internship watcher", copy the 16-character code

University accounts are Google Workspace and usually block app passwords. If
step 2 shows no option, use a **personal Gmail to send** and keep your school
address as the destination — the script asks for these separately.

### Schedule

| When | How often |
|---|---|
| Aug–Jan, weekdays 8am–7pm ET | **every 30 minutes** |
| Aug–Jan, nights and weekends | every 3 hours |
| Feb–Jul | 3× a day |

Aug–Jan is when Summer 2027 postings actually drop. The four cron entries are
verified non-overlapping, so no run is ever triggered twice.

The repo is **public**, which means unlimited free Actions minutes — that's
what makes 30-minute polling free. Nothing sensitive is in it: `.env` is
gitignored, and the real email credentials live in GitHub's encrypted secrets,
which cannot be read back out of the repo. The workflow only triggers on
`schedule` and `workflow_dispatch`, never on `pull_request`, so a stranger's
fork or PR can't reach those secrets.

### Doing it by hand instead

If you'd rather not run the script: create the repo, push this folder,
add `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `EMAIL_FROM` `EMAIL_TO`
under Settings → Secrets and variables → Actions, then run the
`internship-watch` workflow once from the Actions tab.

**No-GitHub alternative** (Windows Task Scheduler — only runs when your laptop
is awake, which is how you miss a posting in October):

```bash
schtasks /create /tn "InternshipWatch" /tr "python %CD%\watcher.py" /sc hourly /mo 3
```

## Why it won't fail silently

The real risk over six months isn't a false alarm — it's the bot quietly
breaking in November and you never noticing. So:

- Any source that errors, 404s, or parses to zero jobs raises instead of
  returning an empty list, and you get a **source health warning** in the email
  naming the broken source.
- A source that returned postings last run and zero this run is flagged as
  probably-broken rather than treated as "nothing new."
- If nothing at all happens for `heartbeat_days` (default 7), it emails you a
  short "still running" note anyway. Silence always means broken, never "quiet."

## Tuning

Everything lives in `config.yaml`.

- `include_any` / `exclude_any` — what counts as an internship. Matching is
  whole-word, so `intern` won't match "International."
- `priority_any` — postings matching these get flagged PRIORITY and sorted to
  the top. Currently `2027`, `sophomore`, `rising junior`, etc.
- `locations_any` — US cities/regions. Postings with no location listed are
  **kept**, not dropped, so you never lose a match to missing metadata.

Bias is deliberately toward over-including. An extra email costs you nothing.

## Adding sources

Supported adapters: `workday`, `successfactors`, `google_careers`,
`github_markdown`, `apmlist`, `greenhouse` (`board:`), `lever` (`company:`),
and `page_watch` (hashes a page and alerts on any change — universal but noisy).

Adding another curated GitHub list is a three-line change; the markdown parser
maps columns by header name, so it handles new layouts without code changes:

```yaml
- name: "Some List"
  type: github_markdown
  repo: owner/repo
  branch: main       # check: some repos still use master
  filter: false      # set when the list is already curated for underclassmen
```

Most banks and consultancies are Workday. To add one, open its careers site,
read the URL `https://{host}/en-US/{site}`, and add:

```yaml
- name: Some Firm
  type: workday
  host: somefirm.wd1.myworkdayjobs.com
  tenant: somefirm
  site: External
```

Note the two Workday quirks already handled: it ignores `searchText`, and it
reports `total: 0` on every page after the first. The adapter pulls the whole
board and filters locally because of this.

## Commands

```bash
python watcher.py                 # normal run
python watcher.py --dry-run       # print, don't send, don't save
python watcher.py --only Visa     # test one source
python watcher.py --test-email    # check SMTP
```

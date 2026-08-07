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

## Setup

### 1. Credentials

```bash
cp .env.example .env
```

Fill in `.env`. For Gmail you need an **App Password**, not your normal
password: enable 2-Step Verification, then go to
<https://myaccount.google.com/apppasswords>.

Your Penn address (`sdawda@sas.upenn.edu`) is Google Workspace and app
passwords are often disabled by university policy. If you don't see the
app-password option, use a personal Gmail as `SMTP_USER`/`EMAIL_FROM` and keep
`EMAIL_TO` as your Penn address.

Verify it works:

```bash
python watcher.py --test-email
```

### 2. First run

```bash
pip install -r requirements.txt
python watcher.py --dry-run --notify-first-run
```

That prints what it would send without emailing or saving anything. When it
looks right, run it for real:

```bash
python watcher.py
```

The first real run **seeds** `state.json` with everything currently open and
deliberately does not email you about it — otherwise you'd get a wall of stale
postings. From then on you only hear about genuinely new ones.

Since a lot of the curated-list roles are open *today*, you probably do want
that first backlog once. Run it with:

```bash
python watcher.py --notify-first-run
```

That's a single ~327-item catalogue email. After that, expect a handful a day.

### 3. Run it automatically (recommended: GitHub Actions)

Your laptop being asleep in October is how you miss a posting. GitHub Actions
runs in the cloud for free.

1. Create a **private** GitHub repo and push this folder to it.
2. Repo → Settings → Secrets and variables → Actions → add `SMTP_HOST`,
   `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `EMAIL_TO`.
3. Actions tab → enable workflows → run `internship-watch` manually once.

It then runs every 3 hours and commits `state.json` back so it remembers what
it has already seen.

**Local alternative** (Windows Task Scheduler) if you'd rather not use GitHub:

```bash
schtasks /create /tn "InternshipWatch" /tr "python C:\Users\shive\OneDrive\Desktop\internship-watcher\watcher.py" /sc hourly /mo 3
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

"""Adapters that turn a company's careers site into a list of Job records.

Each adapter takes a source dict from config.yaml and returns list[Job].

Design rule: an adapter must raise SourceError for anything that means
"I could not read this site." It must never return an empty list to paper over
a failure. Silent breakage is the only failure mode that actually costs you an
internship, so every adapter fails loudly and the watcher emails you about it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin


class SourceError(Exception):
    """Raised when a source cannot be read at all (network, HTTP, parse)."""


# Non-fatal problems: the source returned usable data, but something about it
# deserves telling the user (e.g. a table layout changed and rows are being
# dropped). The watcher drains this after each adapter call and folds the
# messages into the email's health section. Losing rows quietly is exactly the
# failure this whole tool exists to avoid.
WARNINGS: dict[str, list[str]] = {}


def _warn(src: dict, message: str) -> None:
    WARNINGS.setdefault(src.get("name", "?"), []).append(message)


@dataclass(frozen=True)
class Job:
    id: str          # stable per-company id; used to decide "have I seen this?"
    title: str
    url: str
    location: str = ""
    posted: str = ""
    source: str = ""

    def key(self) -> str:
        return f"{self.source}::{self.id}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(text or "")).strip()


def _strip_html(raw: str) -> str:
    return _clean(_ANY_TAG_RE.sub(" ", _TAG_RE.sub(" ", raw)))


def _titleize_slug(slug: str) -> str:
    words = slug.replace("_", "-").split("-")
    small = {"and", "of", "the", "for", "in", "to", "a", "an"}
    out = []
    for i, w in enumerate(words):
        if not w:
            continue
        if w.isdigit() or len(w) <= 3 and w.isalpha() and w.isupper():
            out.append(w)
        elif w in small and i:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


RETRY_STATUS = {429, 500, 502, 503, 504}


def _request(session, method: str, url: str, *, attempts: int = 3, **kw):
    """HTTP with retries.

    Career sites throw transient 502s often enough that a single failure must
    not be reported as "this source is broken" -- that trains you to ignore the
    health warnings, which defeats the point of having them.
    """
    import time

    kw.setdefault("timeout", 40)
    last = ""
    for attempt in range(attempts):
        try:
            r = session.request(method, url, **kw)
        except Exception as exc:  # network, DNS, TLS, timeout
            last = f"{type(exc).__name__}: {exc}"
        else:
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code not in RETRY_STATUS:
                break
        if attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
    raise SourceError(f"{method} {url} failed after {attempts} attempt(s): {last}")


def _get(session, url, **kw):
    return _request(session, "GET", url, **kw)


# --------------------------------------------------------------------------
# Workday  (verified: Mastercard, Visa)
# --------------------------------------------------------------------------

def workday(src: dict, session) -> list[Job]:
    """Workday's public search API.

    Every Workday tenant exposes POST /wday/cxs/{tenant}/{site}/jobs returning
    JSON. Find `host`/`tenant`/`site` by opening the company's Workday careers
    page: https://{host}/en-US/{site} -> tenant is usually the subdomain label.
    """
    host, tenant, site = src["host"], src["tenant"], src["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    page = int(src.get("page_size", 20))
    cap = int(src.get("max_results", 2000))

    # Two Workday quirks we have to work around, both verified against live
    # tenants: `searchText` is ignored (it returns the same unfiltered list),
    # and `total` comes back as 0 on every page after the first. So we ignore
    # server-side search entirely, pull the whole board, and filter locally;
    # pagination stops on the first empty page rather than trusting `total`.
    found: dict[str, Job] = {}
    offset, empty_streak = 0, 0
    while offset < cap:
        payload = {
            "appliedFacets": src.get("facets", {}),
            "limit": page,
            "offset": offset,
            "searchText": "",
        }
        r = _request(session, "POST", api, json=payload)
        try:
            data = r.json()
        except ValueError as exc:
            raise SourceError(f"{api} did not return JSON") from exc

        postings = data.get("jobPostings") or []
        if not postings:
            break

        before = len(found)
        for p in postings:
            path = p.get("externalPath") or ""
            bullets = [b for b in (p.get("bulletFields") or []) if b]
            jid = bullets[0] if bullets else path
            if not jid:
                continue
            found[jid] = Job(
                id=jid,
                title=_clean(p.get("title", "")),
                url=f"https://{host}/en-US/{site}{path}",
                location=_clean(p.get("locationsText", "")),
                posted=_clean(p.get("postedOn", "")),
                source=src["name"],
            )
        # Some tenants loop back to page 1 instead of ending; bail on repeats.
        empty_streak = empty_streak + 1 if len(found) == before else 0
        if empty_streak >= 2:
            break
        offset += page

    if not found:
        raise SourceError("Workday returned 0 postings for every search term")
    return list(found.values())


# --------------------------------------------------------------------------
# SAP SuccessFactors career sites  (verified: EY / EY-Parthenon)
# --------------------------------------------------------------------------

# Attribute order varies between markup variants, so match with lookaheads.
_SF_LINK_RE = re.compile(
    r'<a\b(?=[^>]*class="[^"]*jobTitle-link)(?=[^>]*href="(?P<href>[^"]+)")[^>]*>(?P<title>[^<]*)',
    re.I,
)
_SF_ID_RE = re.compile(r"/job/[^/]+/(\d+)/?")


def successfactors(src: dict, session) -> list[Job]:
    """SuccessFactors 'search' pages render results server-side as HTML.

    Job URLs look like /{prefix}/job/{slug}/{numeric-id}/ and that numeric id is
    stable, which is what we key on.
    """
    base = src["base_url"].rstrip("/")
    found: dict[str, Job] = {}
    for term in src.get("search_terms") or [""]:
        for start in range(0, int(src.get("max_results", 100)), 25):
            url = (
                f"{base}/search/?q={term}"
                f"&sortColumn=referencedate&sortDirection=desc&startrow={start}"
            )
            body = _get(session, url).text
            hits = list(_SF_LINK_RE.finditer(body))
            if not hits:
                break
            for m in hits:
                href = html.unescape(m.group("href"))
                idm = _SF_ID_RE.search(href)
                if not idm:
                    continue
                found[idm.group(1)] = Job(
                    id=idm.group(1),
                    # hrefs are site-absolute ("/ey/job/..."), so join against
                    # the origin -- joining against base duplicates the prefix.
                    url=urljoin(base, href),
                    title=_clean(m.group("title")),
                    source=src["name"],
                )

    if not found:
        raise SourceError("SuccessFactors page parsed but contained no job links")
    return list(found.values())


# --------------------------------------------------------------------------
# Google Careers  (verified)
# --------------------------------------------------------------------------

_GOOGLE_RE = re.compile(r"jobs/results/(?P<id>\d{6,})-(?P<slug>[a-z0-9\-]+)")


def google_careers(src: dict, session) -> list[Job]:
    """Google's careers site is a JS app, but the server-rendered HTML still
    embeds every result as `jobs/results/{id}-{slug}` links. We parse those.

    Tune what you see with `urls` in config (target_level, q, location, etc.).
    """
    found: dict[str, Job] = {}
    max_pages = int(src.get("max_pages", 8))
    for url in src["urls"]:
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in url else "?"
            body = _get(session, f"{url}{sep}page={page}").text
            before = len(found)
            for m in _GOOGLE_RE.finditer(body):
                jid, slug = m.group("id"), m.group("slug")
                found[jid] = Job(
                    id=jid,
                    title=_titleize_slug(slug),
                    url=f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}",
                    source=src["name"],
                )
            if len(found) == before:  # page added nothing new -> end of results
                break

    if not found:
        raise SourceError("Google careers HTML contained no job result links")
    return list(found.values())


# --------------------------------------------------------------------------
# Amazon  (verified) -- amazon.jobs exposes a plain JSON search
# --------------------------------------------------------------------------

def amazon_jobs(src: dict, session) -> list[Job]:
    """amazon.jobs/en/search.json is a public, unauthenticated JSON endpoint.

    It genuinely honours `base_query`, unlike Workday's ignored `searchText`,
    so we can search server-side and stay cheap. `country_code` is returned
    per job (USA / ITA / IND ...), which is a far more reliable location
    signal than free-text city strings.
    """
    page = int(src.get("page_size", 100))
    cap = int(src.get("max_results", 500))
    keep_countries = {c.upper() for c in src.get("countries", ["USA"])}

    found: dict[str, Job] = {}
    any_returned = False
    for term in src.get("search_terms") or ["intern"]:
        offset = 0
        while offset < cap:
            url = (
                "https://www.amazon.jobs/en/search.json"
                f"?base_query={requests_quote(term)}&result_limit={page}&offset={offset}"
                "&sort=recent"
            )
            data = _get(session, url).json()
            jobs = data.get("jobs") or []
            if not jobs:
                break
            any_returned = True
            for j in jobs:
                cc = (j.get("country_code") or "").upper()
                if keep_countries and cc and cc not in keep_countries:
                    continue
                jid = str(j.get("id_icims") or j.get("id") or "")
                path = j.get("job_path") or ""
                if not jid or not path:
                    continue
                found[jid] = Job(
                    id=jid,
                    title=_clean(j.get("title", "")),
                    url=f"https://www.amazon.jobs{path}",
                    location=_clean(j.get("normalized_location", "")),
                    posted=_clean(j.get("posted_date", "")),
                    source=src["name"],
                )
            offset += page

    if not any_returned:
        raise SourceError("amazon.jobs returned no jobs for any search term")
    return list(found.values())  # may be empty if nothing matched the country


def requests_quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s)


# --------------------------------------------------------------------------
# USAJOBS -- every federal internship (Pathways, agency programs)
# --------------------------------------------------------------------------

def usajobs(src: dict, session) -> list[Job]:
    """The official federal job API. Covers essentially all public-sector
    internships, including Pathways.

    Needs a free API key (see README). Ships disabled; enable it after adding
    USAJOBS_KEY and USAJOBS_EMAIL. If the response shape ever changes, this
    raises rather than returning nothing, so you get told.
    """
    import os

    key = os.environ.get("USAJOBS_KEY", "").strip()
    email = os.environ.get("USAJOBS_EMAIL", "").strip()
    if not key or not email:
        raise SourceError(
            "USAJOBS_KEY / USAJOBS_EMAIL not set. Get a free key at "
            "https://developer.usajobs.gov/apirequest/ then add them as "
            "GitHub secrets, or set enabled: false for this source."
        )

    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": email,
        "Authorization-Key": key,
    }
    page_size = int(src.get("page_size", 100))
    found: dict[str, Job] = {}
    any_payload = False

    for term in src.get("search_terms") or ["intern"]:
        page = 1
        while page <= int(src.get("max_pages", 3)):
            url = (
                "https://data.usajobs.gov/api/search"
                f"?Keyword={requests_quote(term)}&ResultsPerPage={page_size}&Page={page}"
            )
            r = _request(session, "GET", url, headers=headers)
            try:
                payload = r.json()
            except ValueError as exc:
                raise SourceError("USAJOBS did not return JSON") from exc

            result = payload.get("SearchResult")
            if result is None:
                raise SourceError("USAJOBS payload missing SearchResult (API changed?)")
            any_payload = True

            items = result.get("SearchResultItems") or []
            if not items:
                break
            for it in items:
                d = it.get("MatchedObjectDescriptor") or {}
                jid = str(it.get("MatchedObjectId") or d.get("PositionID") or "")
                title = _clean(d.get("PositionTitle", ""))
                if not jid or not title:
                    continue
                locs = d.get("PositionLocationDisplay") or ""
                if not locs:
                    names = [l.get("LocationName", "") for l in (d.get("PositionLocation") or [])]
                    locs = ", ".join(n for n in names[:2] if n)
                found[jid] = Job(
                    id=jid,
                    title=title,
                    url=d.get("PositionURI") or "https://www.usajobs.gov",
                    location=_clean(locs),
                    posted=_clean(d.get("PublicationStartDate", ""))[:10],
                    source=src["name"],
                )
            page += 1

    if not any_payload:
        raise SourceError("USAJOBS returned no usable pages")
    return list(found.values())


# --------------------------------------------------------------------------
# Greenhouse / Lever  (for smaller firms you may add later)
# --------------------------------------------------------------------------

def greenhouse(src: dict, session) -> list[Job]:
    board = src["board"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    data = _get(session, url).json()
    jobs = [
        Job(
            id=str(j["id"]),
            title=_clean(j.get("title", "")),
            url=j.get("absolute_url", ""),
            location=_clean((j.get("location") or {}).get("name", "")),
            posted=_clean(j.get("updated_at", "")),
            source=src["name"],
        )
        for j in data.get("jobs", [])
    ]
    if not jobs:
        raise SourceError("Greenhouse board returned no jobs")
    return jobs


def lever(src: dict, session) -> list[Job]:
    company = src["company"]
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    data = _get(session, url).json()
    jobs = [
        Job(
            id=str(j.get("id")),
            title=_clean(j.get("text", "")),
            url=j.get("hostedUrl", ""),
            location=_clean((j.get("categories") or {}).get("location", "")),
            source=src["name"],
        )
        for j in data
    ]
    if not jobs:
        raise SourceError("Lever board returned no jobs")
    return jobs


# --------------------------------------------------------------------------
# Generic fallback: watch a page for change
# --------------------------------------------------------------------------

def page_watch(src: dict, session) -> list[Job]:
    """Universal fallback for sites with no usable API (use this for MBB).

    Two modes:
      * `link_pattern` given -> treat each matching link as a job (preferred).
      * otherwise            -> hash the page text and emit one pseudo-job whose
                                id changes whenever the page changes.

    Hash mode is noisy on pages with rotating banners; prefer `text_selector`
    style narrowing via `strip_before` / `strip_after` markers when you can.
    """
    url = src["url"]
    body = _get(session, url).text

    pattern = src.get("link_pattern")
    if pattern:
        rx = re.compile(pattern, re.I)
        found: dict[str, Job] = {}
        for m in rx.finditer(body):
            groups = m.groupdict()
            jid = groups.get("id") or m.group(0)
            href = groups.get("href") or m.group(0)
            title = _clean(groups.get("title") or _titleize_slug(jid))
            found[jid] = Job(
                id=jid,
                title=title,
                url=urljoin(url, html.unescape(href)),
                source=src["name"],
            )
        if not found:
            raise SourceError(f"link_pattern matched nothing on {url}")
        return list(found.values())

    text = _strip_html(body)
    before, after = src.get("strip_before"), src.get("strip_after")
    if before and before in text:
        text = text.split(before, 1)[1]
    if after and after in text:
        text = text.split(after, 1)[0]
    if not text:
        raise SourceError(f"{url} produced no readable text")

    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return [
        Job(
            id=f"pagehash-{digest}",
            title=f"Careers page changed: {src['name']}",
            url=url,
            source=src["name"],
        )
    ]


# --------------------------------------------------------------------------
# Community-curated GitHub lists  (verified: 4 repos, 4 different schemas)
# --------------------------------------------------------------------------

_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_CLOSED_RE = re.compile(r"(🔒|❌|\bclosed\b|no longer accepting)", re.I)

# Header aliases -> the field we want. Every repo names its columns differently.
_COLS = {
    # --- who is offering it ---
    "company": "company",
    "organization": "company",
    "organisation": "company",
    "university/organization": "company",
    "university": "company",
    "employer": "company",
    "agency": "company",
    "name": "name",
    # --- what it is ---
    "role": "role",
    "position": "role",
    "title": "role",
    "program": "role",
    "programme": "role",
    "opportunity": "role",
    "scholarship": "role",
    "fellowship": "role",
    "internship": "role",
    # --- where ---
    "location": "location",
    "locations": "location",
    "state": "location",
    "application": "link",
    "application/link": "link",
    "apply": "link",
    "link": "link",
    "status": "status",
    "status/open date": "status",
    "date posted": "date",
    "date": "date",
    "age": "date",
    "year": "note",
    "note": "note",
    "notes": "note",
    "description": "note",
    "approximate deadline": "note",
    "deadline": "note",
    "type": "note",
    "field": "note",
    "amount": "note",
    "award": "note",
    "eligibility": "note",
}

# Column names seen in a tracked repo that map to nothing. Any unmapped header
# means rows are being silently dropped, so github_markdown reports it rather
# than quietly parsing a fraction of the file.
_KNOWN_UNMAPPED = {"", "status", "links", "link", "apply", "application"}

# Sentinel meaning "we are inside a table that should be skipped entirely".
_SKIP_TABLE: list[str] = []


def _md_cell_text(cell: str) -> str:
    """Strip markdown links, HTML tags and badge images down to plain text."""
    cell = _MDLINK_RE.sub(r"\1", cell)
    cell = re.sub(r"<img[^>]*>", " ", cell, flags=re.I)
    cell = re.sub(r"</?br\s*/?>", " ", cell, flags=re.I)
    return _clean(_ANY_TAG_RE.sub(" ", cell)).strip("* ")


def _row_url(cells: list[str]) -> str:
    for cell in cells:
        m = _HREF_RE.search(cell) or _MDLINK_RE.search(cell)
        if m:
            return html.unescape(m.group(1) if m.re is _HREF_RE else m.group(2))
    return ""


def github_markdown(src: dict, session) -> list[Job]:
    """Parse job tables out of a community-maintained README.

    These repos are the best early-warning signal for underclassmen roles --
    they are usually updated before a company's own careers page is indexed.
    Each repo uses a different column layout, so columns are mapped by header
    name rather than by position.
    """
    import os

    repo = src["repo"]
    branch = src.get("branch", "main")
    path = src.get("path", "README.md")

    # Prefer the REST API over raw.githubusercontent.com. Anonymous raw fetches
    # are rate-limited to roughly 60/hour per IP, and Actions runners share IPs,
    # so four repos every 30 minutes reliably earns an HTTP 429 -- which then
    # looks like a broken source. The API accepts GITHUB_TOKEN (present in every
    # Actions run) and allows 5000/hour. It is also reachable on networks that
    # block raw.githubusercontent.com outright.
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    api_headers = {"Accept": "application/vnd.github.raw"}
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    api_url = f"https://api.github.com/repos/{repo}/readme"
    if path != "README.md":
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    api_url += f"?ref={branch}"

    try:
        body = _request(session, "GET", api_url, headers=api_headers, attempts=2).text
        url = api_url
    except SourceError as api_exc:
        # Fall back to raw, so a token-less local run still works.
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        try:
            body = _get(session, url).text
        except SourceError as raw_exc:
            raise SourceError(f"{api_exc}; raw fallback also failed: {raw_exc}") from raw_exc

    lines = body.splitlines()
    rows_seen = 0
    found: dict[str, Job] = {}
    header: list[str] | None = None
    last_company = ""
    lost_headers: set[str] = set()

    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]

        if header is None:
            header = [_md_cell_text(c).lower() for c in cells]
            # Whole-table skip: a repo that mixes internships with scholarship
            # listings signals it in the column names ("Amount", "Award").
            skip_words = [w.lower() for w in src.get("skip_tables_with") or []]
            if skip_words and any(w in h for h in header for w in skip_words):
                header = _SKIP_TABLE
                continue
            # If nothing in this header identifies the job, every row under it
            # will be dropped. Say so instead of silently parsing a fraction.
            if not any(_COLS.get(h) in ("company", "role", "name") for h in header):
                unknown = [h for h in header if h not in _KNOWN_UNMAPPED
                           and h not in _COLS]
                if unknown:
                    lost_headers.add(", ".join(unknown))
            continue
        if header is _SKIP_TABLE:
            continue  # inside a table excluded by skip_tables_with
        if set("".join(cells).replace("|", "")) <= set("-: "):
            continue  # separator row

        rows_seen += 1
        field: dict[str, str] = {}
        raw: dict[str, str] = {}
        for i, cell in enumerate(cells):
            if i >= len(header):
                break
            key = _COLS.get(header[i])
            if key:
                field[key] = _md_cell_text(cell)
                raw[key] = cell

        if src.get("skip_closed", True) and _CLOSED_RE.search(line):
            continue

        # Repos use "↳" to mean "same company as the row above". Only those
        # explicit markers inherit -- treating a BLANK company as a
        # continuation mislabelled unrelated rows, e.g. tagging the "Arizona
        # Promise Program" as being from "Optimist International" simply
        # because that name appeared further up the table.
        company = field.get("company", "")
        if company in {"↳", "⤷"} and last_company:
            company = last_company
        elif company:
            last_company = company

        role = field.get("role") or field.get("name") or ""
        if not role and not company:
            continue

        title = f"{company} — {role}" if company and role else (company or role)
        note = field.get("note", "")
        if note and len(note) < 90 and note.lower() not in title.lower():
            title = f"{title} [{note}]"

        link = _row_url([raw.get("link", ""), raw.get("name", ""), line])
        # utm params churn between commits, so key on the text instead.
        ident = re.sub(r"[^a-z0-9]+", "", f"{company}{role}".lower())[:80]
        if not ident:
            continue

        found[ident] = Job(
            id=ident,
            title=title[:220],
            url=link or f"https://github.com/{repo}",
            location=field.get("location", ""),
            posted=field.get("date", ""),
            source=src["name"],
        )

    if not rows_seen:
        raise SourceError(f"no markdown table rows found in {url} (layout changed?)")
    if not found:
        raise SourceError(f"{rows_seen} rows found in {url} but none parsed into jobs")
    if lost_headers:
        _warn(
            src,
            "unrecognised table columns, so some rows are being skipped -- add "
            "them to _COLS in sources.py: "
            + "; ".join(sorted(lost_headers)),
        )
    return list(found.values())


# --------------------------------------------------------------------------
# apmlist.com  (verified) -- tracks APM/PM programs with an explicit status
# --------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)


def apmlist(src: dict, session) -> list[Job]:
    """apmlist.com embeds its whole dataset as Next.js JSON.

    Unlike every other source this one publishes a Status field
    ("Not Yet Open" / "Open" / "Paused"), so we can catch the exact moment a
    program flips open. Status is part of the job id, which means a flip from
    "Not Yet Open" to "Open" reads as a new posting and emails you.

    Caveat: a program that goes Open -> Paused -> Open will not re-alert,
    because that id has already been seen.
    """
    import json

    url = src.get("url", "https://apmlist.com/")
    body = _get(session, url).text
    m = _NEXT_DATA_RE.search(body)
    if not m:
        raise SourceError(f"{url}: __NEXT_DATA__ block not found (site rewritten?)")
    try:
        blob = json.loads(m.group(1))
        buckets = blob["props"]["pageProps"]["opportunities"]
    except (ValueError, KeyError) as exc:
        raise SourceError(f"{url}: unexpected JSON shape ({exc})") from exc

    if not isinstance(buckets, dict) or not buckets:
        raise SourceError(f"{url}: no opportunity buckets in payload")

    wanted_buckets = [b.lower() for b in src.get("buckets", ["internship"])]
    wanted_status = [s.lower() for s in src.get("statuses", ["open"])]

    total = 0
    found: dict[str, Job] = {}
    for bucket, items in buckets.items():
        if bucket.lower() not in wanted_buckets or not isinstance(items, list):
            continue
        total += len(items)
        for it in items:
            company = _clean(it.get("Company", ""))
            status = _clean(it.get("Status", ""))
            kind = _clean(it.get("Type", bucket))
            if not company or status.lower() not in wanted_status:
                continue
            ident = re.sub(r"[^a-z0-9]+", "", f"{company}{kind}{status}".lower())
            found[ident] = Job(
                id=ident,
                title=f"{company} — {kind} is now {status.upper()}",
                url=it.get("URL") or url,
                posted=_clean(it.get("Last Modified", ""))[:10],
                source=src["name"],
            )

    if not total:
        raise SourceError(f"{url}: buckets {wanted_buckets} contained no entries")
    return list(found.values())  # legitimately empty when nothing is Open yet


ADAPTERS = {
    "workday": workday,
    "amazon_jobs": amazon_jobs,
    "usajobs": usajobs,
    "github_markdown": github_markdown,
    "apmlist": apmlist,
    "successfactors": successfactors,
    "google_careers": google_careers,
    "greenhouse": greenhouse,
    "lever": lever,
    "page_watch": page_watch,
}

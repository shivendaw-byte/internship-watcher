"""Email delivery + digest formatting."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate


class NotifyError(Exception):
    pass


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise NotifyError(
            f"Missing required environment variable {name}. "
            "Set it in .env locally or as a GitHub Actions secret."
        )
    return val or ""


def send_email(subject: str, text_body: str, html_body: str) -> None:
    host = _env("SMTP_HOST", "smtp.gmail.com")
    port = int(_env("SMTP_PORT", "587"))
    user = _env("SMTP_USER", required=True)
    password = _env("SMTP_PASS", required=True)
    to_addr = _env("EMAIL_TO", required=True)
    from_addr = _env("EMAIL_FROM", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=45) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=45) as s:
                s.ehlo()
                s.starttls(context=context)
                s.login(user, password)
                s.send_message(msg)
    except Exception as exc:
        raise NotifyError(f"SMTP send failed via {host}:{port} -> {exc}") from exc


# ---------------------------------------------------------------------------
# digest rendering
# ---------------------------------------------------------------------------

_CSS = """
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;margin:0;padding:24px;background:#f6f7f9}
.wrap{max-width:680px;margin:0 auto;background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:24px}
h1{font-size:19px;margin:0 0 4px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:#5b6470;margin:26px 0 10px;border-bottom:1px solid #e3e6ea;padding-bottom:6px}
.job{padding:11px 0;border-bottom:1px solid #f0f2f4}
.job:last-child{border-bottom:none}
.t{font-weight:600;font-size:15px;text-decoration:none;color:#0b57d0}
.meta{color:#5b6470;font-size:13px;margin-top:3px}
.rev{display:inline-block;background:#fff4e5;color:#a35200;font-size:11px;font-weight:700;padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:2px}
.why{color:#a35200;font-size:12px;margin-top:2px}
.pri{display:inline-block;background:#fde7e9;color:#b3261e;font-size:11px;font-weight:700;padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:2px}
.co{display:inline-block;background:#eef1f5;color:#3c4450;font-size:11px;font-weight:600;padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:2px}
.warn{background:#fff4e5;border:1px solid #ffd8a8;border-radius:8px;padding:12px 14px;margin-top:18px;font-size:14px}
.warn b{color:#a35200}
.foot{color:#8a929c;font-size:12px;margin-top:22px;border-top:1px solid #e3e6ea;padding-top:12px}
"""


def _bucket(listings):
    """Split into the three sections the digest shows."""
    pri = [L for L in listings if L.verdict == "match" and L.priority]
    match = [L for L in listings if L.verdict == "match" and not L.priority]
    review = [L for L in listings if L.verdict == "review"]
    return pri, match, review


_FUNC_LABEL = {
    "econ_policy": "econ/policy",
    "business": "business",
    "consulting_adjacent": "econ consulting",
    "unclassified": "unclassified",
}


def render_digest(new_jobs: list, health: list[str], stats: dict) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body).

    `new_jobs` is a list of watcher.Listing. Sections are ordered so the top of
    the email is the part worth acting on, and "needs review" is visibly
    separate -- those are postings the bot could not judge, not weak matches.
    """
    pri, match, review = _bucket(new_jobs)
    n = len(new_jobs)

    if n:
        bits = f"{n} new posting{'s' if n != 1 else ''}"
        lead = f"{len(pri)} PRIORITY + " if pri else ""
        tail = f" ({len(review)} to review)" if review else ""
        subject = f"{lead}{bits}{tail}"
    elif health:
        subject = f"No new postings - {len(health)} source issue(s)"
    else:
        subject = "Still watching - nothing new"

    def line(L):
        fn = _FUNC_LABEL.get(L.function, L.function)
        where = L.job.location or "location not listed"
        return (L.job.title, L.job.url, f"{L.job.source} | {fn} | {where}", L.reason)

    # ---- plaintext -------------------------------------------------------
    out = []
    for title, items in (("PRIORITY - names your class year or cycle", pri),
                         ("CLEAR MATCHES", match),
                         ("NEEDS REVIEW - bot could not confirm eligibility", review)):
        if not items:
            continue
        out.append(title)
        out.append("=" * 60)
        for L in items:
            t, url, meta, reason = line(L)
            out.append(f"* {t}")
            out.append(f"  {meta}")
            if L.verdict == "review":
                out.append(f"  why flagged: {reason}")
            out.append(f"  {url}")
            out.append("")
    if not n:
        out.append("No new postings since the last check.")
        out.append("")
    if health:
        out.append("SOURCE HEALTH WARNINGS -- these are NOT being watched:")
        out += [f"  ! {h}" for h in health]
        out.append("")
    out.append("Checked: " + ", ".join(f"{k} ({v})" for k, v in sorted(stats.items())))
    text_body = "\n".join(out)

    # ---- html ------------------------------------------------------------
    def job_html(L):
        t, url, meta, reason = line(L)
        tag = '<span class="pri">PRIORITY</span>' if L.priority else ""
        if L.verdict == "review":
            tag = '<span class="rev">REVIEW</span>'
        why = (f'<div class="why">why flagged: {reason}</div>'
               if L.verdict == "review" else "")
        return (f'<div class="job">{tag}<a class="t" href="{url}">{t}</a>'
                f'<div class="meta">{meta}</div>{why}</div>')

    h = ['<div class="wrap">', "<h1>Internship watch</h1>"]
    for title, items in (("Priority &mdash; names your class year", pri),
                         ("Clear matches", match),
                         ("Needs review &mdash; eligibility unconfirmed", review)):
        if not items:
            continue
        h.append(f"<h2>{title}</h2>")
        h += [job_html(L) for L in items]
    if not n:
        h.append("<p>No new postings since the last check. "
                 "This email confirms the watcher is still running.</p>")
    if health:
        h.append('<div class="warn"><b>Source health warnings</b><br>'
                 "These returned nothing usable, so they are <b>not being "
                 "watched</b> until fixed:<ul>")
        h += [f"<li>{item}</li>" for item in health]
        h.append("</ul></div>")
    h.append('<div class="foot">Checked: '
             + ", ".join(f"{k} ({v})" for k, v in sorted(stats.items()))
             + "</div></div>")
    html_body = f"<html><head><style>{_CSS}</style></head><body>{''.join(h)}</body></html>"

    return subject, text_body, html_body

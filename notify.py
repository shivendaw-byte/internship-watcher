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
.pri{display:inline-block;background:#fde7e9;color:#b3261e;font-size:11px;font-weight:700;padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:2px}
.co{display:inline-block;background:#eef1f5;color:#3c4450;font-size:11px;font-weight:600;padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:2px}
.warn{background:#fff4e5;border:1px solid #ffd8a8;border-radius:8px;padding:12px 14px;margin-top:18px;font-size:14px}
.warn b{color:#a35200}
.foot{color:#8a929c;font-size:12px;margin-top:22px;border-top:1px solid #e3e6ea;padding-top:12px}
"""


def render_digest(new_jobs: list, health: list[str], stats: dict) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body)."""
    priority = [j for j, p in new_jobs if p]
    normal = [j for j, p in new_jobs if not p]

    if new_jobs:
        bits = f"{len(new_jobs)} new posting{'s' if len(new_jobs) != 1 else ''}"
        subject = f"{len(priority)} PRIORITY + {bits}" if priority else bits
    elif health:
        subject = f"No new postings - {len(health)} source issue(s)"
    else:
        subject = "Still watching - nothing new"

    # ---- plaintext -------------------------------------------------------
    lines = []
    if priority:
        lines.append("PRIORITY (mentions 2027 / sophomore / rising junior)")
        lines.append("=" * 55)
        for j in priority:
            lines += [f"* [{j.source}] {j.title}",
                      f"  {j.location or 'location not listed'}  {j.posted}".rstrip(),
                      f"  {j.url}", ""]
    if normal:
        lines.append("OTHER NEW INTERNSHIP POSTINGS")
        lines.append("=" * 55)
        for j in normal:
            lines += [f"* [{j.source}] {j.title}",
                      f"  {j.location or 'location not listed'}  {j.posted}".rstrip(),
                      f"  {j.url}", ""]
    if not new_jobs:
        lines.append("No new internship postings since the last check.")
        lines.append("")
    if health:
        lines.append("SOURCE HEALTH WARNINGS -- these sources are NOT being watched:")
        for h in health:
            lines.append(f"  ! {h}")
        lines.append("")
    lines.append(
        "Checked: " + ", ".join(f"{k} ({v})" for k, v in sorted(stats.items()))
    )
    text_body = "\n".join(lines)

    # ---- html ------------------------------------------------------------
    def job_html(j, is_pri):
        tag = '<span class="pri">PRIORITY</span>' if is_pri else ""
        meta = " &middot; ".join(
            x for x in [j.location or "location not listed", j.posted] if x
        )
        return (
            f'<div class="job">{tag}<span class="co">{j.source}</span>'
            f'<a class="t" href="{j.url}">{j.title}</a>'
            f'<div class="meta">{meta}</div></div>'
        )

    h = ['<div class="wrap">']
    h.append("<h1>Internship watch</h1>")
    if priority:
        h.append("<h2>Priority &mdash; matches your class year</h2>")
        h += [job_html(j, True) for j in priority]
    if normal:
        h.append("<h2>Other new internship postings</h2>")
        h += [job_html(j, False) for j in normal]
    if not new_jobs:
        h.append(
            "<p>No new internship postings since the last check. "
            "This email confirms the watcher is still running.</p>"
        )
    if health:
        h.append('<div class="warn"><b>Source health warnings</b><br>')
        h.append(
            "These sources returned nothing usable, so they are effectively "
            "<b>not being watched</b> until fixed:<ul>"
        )
        h += [f"<li>{item}</li>" for item in health]
        h.append("</ul></div>")
    h.append(
        '<div class="foot">Checked: '
        + ", ".join(f"{k} ({v})" for k, v in sorted(stats.items()))
        + "</div></div>"
    )
    html_body = f"<html><head><style>{_CSS}</style></head><body>{''.join(h)}</body></html>"

    return subject, text_body, html_body

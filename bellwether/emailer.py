"""Optional email delivery of briefing memos.

One email per run, rendered as HTML so memos read like briefings rather
than log output, with a plain text alternative for clients that refuse
HTML. Delivery is layered on top of the real record: memos are always
written to disk first, so a failed send loses nothing, and the failure
is returned rather than raised. A run must never die at delivery.
"""

from __future__ import annotations

import html
import re
import smtplib
from email.message import EmailMessage

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS

SECTIONS = [
    "SUBJECT", "WHAT CHANGED", "SUPPORTING NUMBERS", "INVESTIGATION",
    "LIKELY EXPLANATION", "CONFIDENCE", "SOURCES",
]
_SECTION_RE = re.compile(
    r"^(" + "|".join(SECTIONS) + r")\s*:\s*", re.MULTILINE
)

_COLOURS = {"high": "#1a7f37", "medium": "#9a6700", "low": "#57606a"}


def _split_sections(memo: str) -> list[tuple[str, str]]:
    """Split a memo into (label, body) pairs, tolerating missing sections."""
    parts = _SECTION_RE.split(memo)
    pairs = []
    for i in range(1, len(parts) - 1, 2):
        body = parts[i + 1].strip()
        if body:
            pairs.append((parts[i], body))
    return pairs


def _body_html(label: str, body: str) -> str:
    safe = html.escape(body)
    lines = [ln.strip() for ln in safe.split("\n") if ln.strip()]
    if label in ("SUPPORTING NUMBERS", "SOURCES") and len(lines) > 1:
        items = "".join(
            f"<li style='margin:2px 0;'>{ln.lstrip('-').strip()}</li>"
            for ln in lines
        )
        return f"<ul style='margin:4px 0 0 18px;padding:0;'>{items}</ul>"
    return "<br>".join(lines)


def _memo_html(item: dict) -> str:
    memo = item["memo"]
    sections = _split_sections(memo)
    subject = next((b for l, b in sections if l == "SUBJECT"), item["summary"])
    confidence = next((b for l, b in sections if l == "CONFIDENCE"), "")
    level = next((k for k in _COLOURS if confidence.lower().startswith(k)),
                 "low")

    verified = item.get("verified")
    badge_colour = "#1a7f37" if verified else "#9a6700"
    badge_text = ("all figures traced to evidence" if verified
                  else "figures not fully traced")

    rows = []
    for label, body in sections:
        if label in ("SUBJECT", "CONFIDENCE"):
            continue
        rows.append(
            "<tr>"
            "<td style='padding:6px 12px 6px 0;vertical-align:top;"
            "font-size:12px;letter-spacing:0.4px;color:#57606a;"
            f"white-space:nowrap;'>{html.escape(label)}</td>"
            "<td style='padding:6px 0;vertical-align:top;font-size:14px;"
            f"color:#1f2328;'>{_body_html(label, body)}</td>"
            "</tr>"
        )
    if not rows:  # a memo that did not follow the format, shown verbatim
        rows.append(
            "<tr><td colspan='2' style='font-size:14px;white-space:pre-wrap;'>"
            f"{html.escape(memo)}</td></tr>"
        )

    return (
        "<div style='border:1px solid #d0d7de;border-radius:8px;"
        "padding:16px 18px;margin:0 0 18px 0;'>"
        f"<div style='font-size:16px;font-weight:600;color:#1f2328;'>"
        f"{html.escape(subject)}</div>"
        "<div style='margin:8px 0 12px 0;'>"
        f"<span style='background:{_COLOURS[level]};color:#ffffff;"
        "border-radius:10px;padding:2px 9px;font-size:12px;'>"
        f"confidence: {level}"
        "</span>"
        f"<span style='background:{badge_colour};color:#ffffff;"
        "border-radius:10px;padding:2px 9px;font-size:12px;margin-left:6px;'>"
        f"{badge_text}</span>"
        "</div>"
        "<table style='border-collapse:collapse;width:100%;'>"
        + "".join(rows) +
        "</table></div>"
    )

def _quarter_label(quarter: str) -> str:
    """2023-12-31 becomes Q4 2023, the way a person would say it."""
    try:
        year, month, _ = quarter.split("-")
        return f"Q{(int(month) - 1) // 3 + 1} {year}"
    except (ValueError, AttributeError):
        return quarter


def _headline(memo: dict, limit: int = 46) -> str:
    """The shortest honest description of one finding, for a subject line."""
    sections = _split_sections(memo["memo"])
    text = next((b for l, b in sections if l == "SUBJECT"), memo["summary"])
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "..."
    return text


def build_subject(run_id: int, quarter: str, memos: list[dict]) -> str:
    """Lead with the findings, not with our run numbering."""
    head = _headline(memos[0])
    more = f" +{len(memos) - 1} more" if len(memos) > 1 else ""
    return f"[BELLWETHER] {head}{more} | {_quarter_label(quarter)}"

def send_memos(
    address: str,
    app_password: str,
    to: str,
    run_id: int,
    quarter: str,
    memos: list[dict],
) -> dict:
    """Send one email containing all of a run's memos.

    Each memo is a dict with summary, memo, verified, and stop_reason.
    Returns the same ok/error contract the tools use, so the caller logs
    the outcome instead of handling exceptions.
    """
    try:
        if not memos:
            return {"ok": False, "error": "no memos to send"}

        msg = EmailMessage()
        msg["From"] = f"bellwether-agent <{address}>"
        msg["To"] = to
        msg["Subject"] = build_subject(run_id, quarter, memos)

        divider = "\n\n" + "=" * 60 + "\n\n"
        msg.set_content(
            f"bellwether-agent run {run_id}, quarter examined {quarter}.\n"
            f"{len(memos)} memos.\n\n"
            + divider.join(f"{m['summary']}\n\n{m['memo']}" for m in memos)
        )

        verified_count = sum(1 for m in memos if m.get("verified"))
        msg.add_alternative(
            "<div style='font-family:-apple-system,Segoe UI,Helvetica,"
            "Arial,sans-serif;max-width:720px;margin:0 auto;'>"
            "<div style='border-bottom:2px solid #1f2328;padding-bottom:10px;"
            "margin-bottom:18px;'>"
            "<div style='font-size:18px;font-weight:700;'>"
            "bellwether-agent briefing</div>"
            "<div style='font-size:13px;color:#57606a;margin-top:4px;'>"
            f"{html.escape(_quarter_label(quarter))} holdings "
            f"&middot; {len(memos)} memo{'s' if len(memos) != 1 else ''} "
            f"&middot; {verified_count} of {len(memos)} fully traced to "
            f"evidence &middot; run {run_id}"
            "</div></div>"
            + "".join(_memo_html(m) for m in memos) +
            "<div style='font-size:12px;color:#57606a;border-top:1px solid "
            "#d0d7de;padding-top:10px;'>Generated automatically from 13F "
            "filings data. 13F holdings are quarterly and filed up to 45 days "
            "after quarter end, so nothing here is current. A plausible "
            "explanation is not a verified cause.</div></div>",
            subtype="html",
        )

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(address, app_password)
            smtp.send_message(msg)
        return {"ok": True, "sent_to": to, "memo_count": len(memos)}
    except Exception as e:
        return {"ok": False, "error": f"email delivery failed: {e}"}

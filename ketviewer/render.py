"""Render a parsed `Record` as a self-contained HTML document or plain text."""

from __future__ import annotations

import base64
from html import escape

from .model import Field, Payload, Record, format_datetime, format_nhs_number
from .sanitize import sanitize_html, text_to_html

CSS = """
:root{
  --bg:#f5f6f8; --surface:#fff; --ink:#15181d; --muted:#5b6472; --line:#dfe3ea;
  --accent:#005eb8; --accent-soft:#e8f0fa; --warn-bg:#fff4e5; --warn-ink:#8a5200;
  --bad-bg:#fdecec; --bad-ink:#a4262c; --good:#0b7a4b;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#12151a; --surface:#1a1e25; --ink:#e8eaee; --muted:#9aa4b2; --line:#2b313b;
    --accent:#6ba7e8; --accent-soft:#1d2836; --warn-bg:#3a2c14; --warn-ink:#e9be7a;
    --bad-bg:#3a1e20; --bad-ink:#f2a0a4; --good:#5bc48d;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
a{color:var(--accent)}
.wrap{max-width:900px;margin:0 auto;padding-block:24px;padding-left:20px;padding-right:20px}
header.rec{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:18px;border-top:4px solid var(--accent)}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0}
.ids{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:14px;font-size:14px}
.ids div{min-width:120px}
.ids .k{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;
  letter-spacing:.06em}
.ids .v{font-variant-numeric:tabular-nums}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;
  background:var(--accent-soft);color:var(--accent);vertical-align:1px}
.badge.bad{background:var(--bad-bg);color:var(--bad-ink)}
.badge.ok{background:transparent;color:var(--good);padding-left:0}
section.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:0 0 12px;font-weight:600}
h3.group{font-size:13px;color:var(--muted);margin:14px 0 6px;font-weight:600}
dl{display:grid;grid-template-columns:minmax(140px,30%) 1fr;gap:6px 16px;margin:0}
dt{color:var(--muted);font-size:13px}
dd{margin:0;overflow-wrap:anywhere}
dd .raw{color:var(--muted);font-size:12px}
.payload{font-size:15px}
.payload h1,.payload h2,.payload h3,.payload h4{font-size:16px;text-transform:none;
  letter-spacing:0;color:var(--ink);margin:18px 0 6px;font-weight:650}
.payload h3:first-child,.payload h1:first-child{margin-top:0}
.payload p{margin:0 0 10px}
.payload table{border-collapse:collapse;width:100%;margin:10px 0}
.payload th,.payload td{border:1px solid var(--line);padding:6px 9px;text-align:left;
  vertical-align:top}
.payload pre.plain{white-space:pre-wrap;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
  margin:0}
.notice{background:var(--warn-bg);color:var(--warn-ink);border-radius:8px;padding:10px 14px;
  font-size:13px;margin-bottom:16px}
.attach{display:flex;flex-wrap:wrap;align-items:center;gap:12px;font-size:14px}
a.btn{background:var(--accent);color:#fff;text-decoration:none;padding:7px 14px;
  border-radius:7px;font-size:14px;display:inline-block}
details{margin-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
pre.xml{white-space:pre-wrap;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;
  overflow-x:auto;margin-top:10px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:8px 0 24px}
@media print{
  :root{--bg:#fff;--surface:#fff;--line:#ccc;--ink:#000;--muted:#444}
  body{font-size:11pt}
  .wrap{max-width:none;padding:0}
  section.card,header.rec{break-inside:avoid;border-radius:0}
  details{display:none}
}
"""


def render_record(
    record: Record,
    *,
    attachment_urls: dict[int, str] | None = None,
    embed_attachments: bool = True,
    raw_xml: str | None = None,
) -> str:
    """Build a complete, self-contained HTML document for one record."""
    body = render_body(
        record,
        attachment_urls=attachment_urls,
        embed_attachments=embed_attachments,
        raw_xml=raw_xml,
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{escape(record.title)}</title><style>{CSS}</style></head>"
        f"<body><div class='wrap'>{body}</div></body></html>"
    )


def render_body(
    record: Record,
    *,
    attachment_urls: dict[int, str] | None = None,
    embed_attachments: bool = True,
    raw_xml: str | None = None,
    back_link: str | None = None,
) -> str:
    parts: list[str] = []
    if back_link:
        parts.append(f"<p class='sub'><a href='{escape(back_link)}'>&larr; All records</a></p>")
    parts.append(_header(record))
    for warning in record.warnings:
        parts.append(f"<div class='notice'>{escape(warning)}</div>")
    parts.append(_payloads(record, attachment_urls, embed_attachments))
    parts.extend(_section(s) for s in record.sections)
    if raw_xml:
        parts.append(
            "<details><summary>Show source XML</summary>"
            f"<pre class='xml'>{escape(raw_xml)}</pre></details>"
        )
    parts.append(
        "<footer>Rendered locally by ketviewer — payload markup is sanitised "
        "and no external resources are loaded.</footer>"
    )
    return "".join(parts)


# --------------------------------------------------------------------------


def _header(record: Record) -> str:
    nhs = format_nhs_number(record.nhs_number) or "—"
    valid = record.nhs_number_valid
    if valid is True:
        nhs += " <span class='badge ok'>&#10003;</span>"
    elif valid is False:
        nhs += " <span class='badge bad'>check digit fails</span>"

    dob = format_datetime(record.date_of_birth) or "—"
    if record.age_at_encounter:
        dob += f" <span class='sub'>({record.age_at_encounter})</span>"

    items = [
        ("NHS number", nhs),
        ("Date of birth", dob),
        ("Gender", escape(record.gender or "—")),
        ("Encounter", escape(format_datetime(record.encounter_datetime) or "—")),
        ("Clinician", escape(record.clinician or "—")),
        ("Source", escape(record.source_system or record.root_tag or "—")),
    ]
    cells = "".join(
        f"<div><span class='k'>{escape(k)}</span><span class='v'>{v}</span></div>"
        for k, v in items
    )
    received = format_datetime(record.created_datetime)
    sub = " · ".join(
        filter(
            None,
            [
                escape(record.root_tag) if record.root_tag else None,
                f"message {escape(record.message_id)}" if record.message_id else None,
                f"sent {escape(received)}" if received else None,
            ],
        )
    )
    return (
        "<header class='rec'>"
        f"<h1>{escape(record.patient_name)}</h1>"
        f"<p class='sub'>{sub}</p>"
        f"<div class='ids'>{cells}</div></header>"
    )


def _payloads(
    record: Record, attachment_urls: dict[int, str] | None, embed: bool
) -> str:
    if not record.payloads:
        return ""
    blocks = []
    for payload in record.payloads:
        blocks.append(
            "<section class='card'>"
            f"<h2>Clinical content{_payload_suffix(record, payload)}</h2>"
            f"{_payload_body(payload, attachment_urls, embed)}"
            "</section>"
        )
    return "".join(blocks)


def _payload_suffix(record: Record, payload: Payload) -> str:
    if len(record.payloads) == 1:
        return ""
    return f" {payload.index + 1} of {len(record.payloads)}"


def _payload_body(
    payload: Payload, attachment_urls: dict[int, str] | None, embed: bool
) -> str:
    if payload.is_binary:
        return _attachment(payload, attachment_urls, embed)
    text = payload.text or ""
    if payload.is_html or "<" in text:
        safe, removed = sanitize_html(text)
        note = ""
        if removed:
            dropped = ", ".join(sorted(removed))
            note = (
                "<div class='notice'>Removed unsupported markup from the sender: "
                f"{escape(dropped)}. Open the source XML below to see it verbatim.</div>"
            )
        return f"{note}<div class='payload'>{safe}</div>"
    return f"<div class='payload'>{text_to_html(text)}</div>"


def _attachment(
    payload: Payload, attachment_urls: dict[int, str] | None, embed: bool
) -> str:
    name = f"attachment-{payload.index + 1}{payload.extension}"
    href = (attachment_urls or {}).get(payload.index)
    if href is None and embed and payload.data is not None:
        encoded = base64.b64encode(payload.data).decode("ascii")
        href = f"data:{payload.content_type};base64,{encoded}"
    link = (
        f"<a class='btn' href='{escape(href, quote=True)}' download='{name}'>Open attachment</a>"
        if href
        else ""
    )
    return (
        "<div class='attach'>"
        f"<span>{escape(payload.description)}</span>{link}</div>"
    )


def _section(section) -> str:
    rows: list[str] = []
    current_group = ""
    for f in section.fields:
        if f.group != current_group:
            current_group = f.group
            if current_group:
                rows.append(f"</dl><h3 class='group'>{escape(_label(current_group))}</h3><dl>")
        rows.append(f"<dt>{escape(f.label)}</dt><dd>{_value(f)}</dd>")
    return (
        f"<section class='card'><h2>{escape(section.title)}</h2>"
        f"<dl>{''.join(rows)}</dl></section>"
    )


def _label(group_path: str) -> str:
    from .model import humanise

    return " / ".join(humanise(p) for p in group_path.split("/"))


def _value(f: Field) -> str:
    """Show a readable value, keeping the coded original when we translate it."""
    from .model import GENDER_CODES

    raw = f.value
    tag = f.tag.lower()
    pretty: str | None = None
    if tag in {"gender", "sex", "personstatedgender"}:
        pretty = GENDER_CODES.get(raw.strip().lower())
    elif "date" in tag or tag in {"dob", "birthdate"}:
        pretty = format_datetime(raw)
    elif tag in {"nhsnumber", "nhsno"}:
        pretty = format_nhs_number(raw)

    if pretty and pretty != raw:
        return f"{escape(pretty)} <span class='raw'>({escape(raw)})</span>"
    if not raw and f.attributes:
        return "<span class='raw'>" + escape(
            ", ".join(f"{k}={v}" for k, v in f.attributes.items())
        ) + "</span>"
    return escape(raw) or "<span class='raw'>—</span>"


# --------------------------------------------------------------------------
# terminal output


def render_text(record: Record, width: int = 88) -> str:
    """A plain-text rendering for quick review in a terminal."""
    lines: list[str] = []
    rule = "=" * width

    def header(title: str) -> None:
        lines.append("")
        lines.append(title.upper())
        lines.append("-" * len(title))

    lines.append(rule)
    lines.append(record.patient_name)
    nhs = format_nhs_number(record.nhs_number) or "—"
    if record.nhs_number_valid is False:
        nhs += "  [!] check digit fails"
    lines.append(
        f"NHS {nhs}   DOB {format_datetime(record.date_of_birth) or '—'}"
        f"{' (' + record.age_at_encounter + ')' if record.age_at_encounter else ''}"
        f"   {record.gender or ''}"
    )
    lines.append(
        f"Encounter {format_datetime(record.encounter_datetime) or '—'}"
        f"   {record.clinician or ''}   {record.source_system or ''}"
    )
    lines.append(rule)

    for warning in record.warnings:
        lines.append(f"[!] {warning}")

    for payload in record.payloads:
        header("Clinical content")
        if payload.is_binary:
            lines.append(f"[{payload.description} — use --save-attachments to extract]")
        else:
            lines.append(_wrap(_plain(payload.text or ""), width))

    for section in record.sections:
        header(section.title)
        width_label = max((len(f.label) for f in section.fields), default=0)
        group = ""
        for f in section.fields:
            if f.group != group:
                group = f.group
                if group:
                    lines.append(f"  [{_label(group)}]")
            lines.append(f"  {f.label.ljust(width_label)}  {f.value}")
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> str:
    import textwrap

    out: list[str] = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, width=width) or [""])
    return "\n".join(out)


def _plain(markup: str) -> str:
    """Flatten payload markup to readable text without losing block structure."""
    import re
    from html import unescape

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|tr|li)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "  • ", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

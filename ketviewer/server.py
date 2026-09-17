"""A local review server for .ket messages.

Binds to the loopback interface only and serves nothing outside the folder it
was pointed at: clinical records stay on the machine they are reviewed on.
Files uploaded through the page are held in memory for the life of the process
and are never written to disk.
"""

from __future__ import annotations

import hashlib
import html
import http.server
import socket
import socketserver
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from email.parser import BytesParser
from pathlib import Path

from .model import Record, format_datetime, format_nhs_number, parse_datetime
from .parser import KetParseError, parse_bytes, parse_file
from .render import CSS, render_body

#: Refuse uploads larger than this. A .ket with a scanned PDF is rarely >5 MB.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

INDEX_CSS = CSS + """
table.index{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
table.index th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  text-align:left;padding:10px 14px;border-bottom:1px solid var(--line)}
table.index td{padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px;
  vertical-align:top}
table.index tr:last-child td{border-bottom:0}
table.index a{color:var(--ink);text-decoration:none;font-weight:600}
table.index a:hover{color:var(--accent)}
td.meta{color:var(--muted);font-size:13px;white-space:nowrap}
form.search{margin:0 0 12px}
form.search input{width:100%;padding:10px 14px;border-radius:8px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink);font-size:15px}
form.upload{background:var(--surface);border:1px dashed var(--line);border-radius:10px;
  padding:14px 16px;margin:0 0 16px;display:flex;flex-wrap:wrap;align-items:center;gap:12px}
form.upload input[type=file]{font-size:14px;color:var(--muted);flex:1 1 220px;min-width:0}
form.upload button{background:var(--accent);color:#fff;border:0;border-radius:7px;
  padding:8px 16px;font-size:14px;cursor:pointer}
form.upload .hint{flex-basis:100%;color:var(--muted);font-size:12px;margin:0}
.count{color:var(--muted);font-size:13px;margin:0 0 14px}
.err{color:var(--bad-ink)}
.tag{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--accent-soft);
  color:var(--accent)}
@media (max-width:640px){td.meta{white-space:normal}}
"""


@dataclass
class Entry:
    """One reviewable message: a file on disk, or an upload held in memory."""

    id: str
    name: str
    record: Record | None
    path: Path | None = None
    data: bytes | None = None
    error: str | None = None
    received: datetime | None = None

    @property
    def is_upload(self) -> bool:
        return self.path is None

    def raw(self) -> bytes:
        if self.data is not None:
            return self.data
        return self.path.read_bytes() if self.path else b""


class Library:
    """The .ket files in a folder, plus anything uploaded this session."""

    def __init__(self, root: Path, pattern: str = "*.ket") -> None:
        self.root = root.resolve()
        self.pattern = pattern
        self._cache: dict[Path, tuple[float, Entry]] = {}
        self.uploads: list[Entry] = []

    # -- disk ------------------------------------------------------------
    def files(self) -> list[Path]:
        if self.root.is_file():
            return [self.root]
        return sorted(p for p in self.root.rglob(self.pattern) if p.is_file())

    def _disk_entries(self) -> list[Entry]:
        out: list[Entry] = []
        for path in self.files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                out.append(cached[1])
                continue
            entry_id = "f" + hashlib.sha1(str(path).encode()).hexdigest()[:12]
            try:
                entry = Entry(id=entry_id, name=path.name, record=parse_file(path), path=path)
            except KetParseError as exc:
                entry = Entry(
                    id=entry_id, name=path.name, record=None, path=path, error=str(exc)
                )
            self._cache[path] = (mtime, entry)
            out.append(entry)
        return out

    # -- uploads ---------------------------------------------------------
    def add_upload(self, filename: str, data: bytes) -> Entry:
        """Parse an uploaded file and keep it in memory. Never touches disk."""
        name = Path(filename or "uploaded.ket").name or "uploaded.ket"
        entry_id = f"u{len(self.uploads) + 1}"
        try:
            record = parse_bytes(data)
            record.source_path = None
            entry = Entry(
                id=entry_id,
                name=name,
                record=record,
                data=data,
                received=datetime.now(),
            )
        except KetParseError as exc:
            entry = Entry(
                id=entry_id,
                name=name,
                record=None,
                data=data,
                error=str(exc),
                received=datetime.now(),
            )
        self.uploads.append(entry)
        return entry

    # -- combined --------------------------------------------------------
    def entries(self) -> list[Entry]:
        combined = self._disk_entries() + self.uploads
        combined.sort(key=_sort_key, reverse=True)
        return combined

    def by_id(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries() if e.id == entry_id), None)


def _sort_key(entry: Entry):
    record = entry.record
    when = None
    if record:
        when = parse_datetime(record.encounter_datetime) or parse_datetime(
            record.created_datetime
        )
    # Uploads float to the top: they are what the reviewer just asked to see.
    return (entry.is_upload, when is not None, when or datetime.min, entry.name)


class Handler(http.server.BaseHTTPRequestHandler):
    library: Library  # set by serve()
    server_version = "ketviewer"
    sys_version = ""

    # -- GET -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)

        if not parts:
            self._send_html(_index_page(self.library.entries(), (query.get("q") or [""])[0]))
            return

        if parts[0] == "record" and len(parts) >= 2:
            entry = self.library.by_id(parts[1])
            if entry is None:
                self._send_error_page(404, "That record is no longer available.")
                return
            if len(parts) == 2:
                if entry.record is None:
                    self._send_error_page(
                        400, f"{entry.name}: {entry.error or 'unreadable file'}"
                    )
                    return
                self._send_html(_record_page(entry))
                return
            if parts[2] == "raw":
                self._send_bytes(entry.raw(), "text/plain; charset=utf-8", inline=True)
                return
            if parts[2] == "attachment" and len(parts) == 4 and parts[3].isdigit():
                self._send_attachment(entry, int(parts[3]))
                return

        self._send_error_page(404, "Not found.")

    # -- POST ------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/upload":
            self._send_error_page(404, "Not found.")
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._send_error_page(400, "Expected a file upload.")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_error_page(400, "Nothing was uploaded.")
            return
        if length > MAX_UPLOAD_BYTES:
            # Read the body away before replying, or the browser sees a reset
            # connection instead of the explanation.
            self._drain(length)
            self._send_error_page(
                413, f"That file is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            )
            return

        body = self.rfile.read(length)
        filename, data = _first_file(content_type, body)
        if data is None:
            self._send_error_page(400, "No file was attached to that upload.")
            return

        entry = self.library.add_upload(filename, data)
        # Redirect rather than render, so a refresh does not re-upload. The
        # record id carries no patient detail, unlike the filename.
        self.send_response(303)
        self.send_header("Location", f"/record/{entry.id}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _drain(self, length: int, cap: int = 4 * MAX_UPLOAD_BYTES) -> None:
        """Discard an over-long request body, giving up on the absurd."""
        remaining = min(length, cap)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        self.close_connection = True

    # -- plumbing --------------------------------------------------------
    def _send_attachment(self, entry: Entry, which: int) -> None:
        if entry.record is None:
            self._send_error_page(404, "No such attachment.")
            return
        match = next(
            (p for p in entry.record.payloads if p.is_binary and p.index == which), None
        )
        if match is None or match.data is None:
            self._send_error_page(404, "No such attachment.")
            return
        # Rendered in place by the browser (PDF, images), so it is served
        # inline rather than as a forced download.
        self._send_bytes(match.data, match.content_type, inline=True)

    def _send_html(self, body: str) -> None:
        self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", inline=True)

    def _send_bytes(self, payload: bytes, content_type: str, inline: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if not inline:
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_page(self, code: int, message: str) -> None:
        body = _page(
            "ketviewer",
            f"<section class='card'><p class='err'>{html.escape(message)}</p>"
            "<p><a href='/'>&larr; All records</a></p></section>",
        )
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        return


#: Same-origin only: no third-party anything. 'frame-ancestors self' lets a
#: record page frame its own attachment while still refusing any other site.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "frame-src 'self'; object-src 'none'; frame-ancestors 'self'; form-action 'self'"
)


def _first_file(content_type: str, body: bytes) -> tuple[str, bytes | None]:
    """Pull the first uploaded file out of a multipart/form-data body."""
    message = BytesParser().parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        return "", None
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True)
        if filename and payload:
            return filename, payload
    return "", None


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{html.escape(title)}</title><style>{INDEX_CSS}</style></head>"
        f"<body><div class='wrap'>{body}</div></body></html>"
    )


def _index_page(entries: list[Entry], query: str) -> str:
    needle = query.strip().lower()
    rows: list[str] = []
    shown = 0
    for entry in entries:
        record = entry.record
        haystack = " ".join(
            filter(
                None,
                [
                    entry.name,
                    record.patient_name if record else "",
                    record.nhs_number if record else "",
                    record.source_system if record else "",
                    record.clinician if record else "",
                ],
            )
        ).lower()
        if needle and needle not in haystack:
            continue
        shown += 1
        tag = " <span class='tag'>uploaded</span>" if entry.is_upload else ""
        if record is None:
            rows.append(
                f"<tr><td colspan='4'><span class='err'>{html.escape(entry.name)} — "
                f"{html.escape(entry.error or 'unreadable')}</span>{tag}</td></tr>"
            )
            continue
        nhs = format_nhs_number(record.nhs_number) or "—"
        when = (
            format_datetime(record.encounter_datetime)
            or format_datetime(record.created_datetime)
            or "—"
        )
        rows.append(
            f"<tr><td><a href='/record/{entry.id}'>{html.escape(record.patient_name)}</a>"
            f"<div class='meta'>{html.escape(entry.name)}{tag}</div></td>"
            f"<td class='meta'>{html.escape(nhs)}</td>"
            f"<td class='meta'>{html.escape(when)}</td>"
            f"<td class='meta'>{html.escape(record.source_system or record.root_tag)}</td></tr>"
        )

    table = (
        "<table class='index'><tr><th>Patient</th><th>NHS number</th>"
        f"<th>Encounter</th><th>Source</th></tr>{''.join(rows)}</table>"
        if rows
        else "<section class='card'><p>No matching records.</p></section>"
    )
    body = (
        "<header class='rec'><h1>Clinical records</h1>"
        f"<p class='sub'>{html.escape(str(Handler.library.root))} · proof of concept</p></header>"
        "<form class='upload' method='post' action='/upload' enctype='multipart/form-data'>"
        "<input type='file' name='file' accept='.ket,.xml,text/xml,application/xml' required>"
        "<button type='submit'>Upload and view</button>"
        "<p class='hint'>Held in memory for this session only — never written to disk, "
        "and gone when the server stops.</p></form>"
        "<form class='search' method='get' action='/'>"
        f"<input name='q' value='{html.escape(query)}' placeholder='Search patient, NHS number, "
        "clinician or filename'></form>"
        f"<p class='count'>{shown} of {len(entries)} record(s)</p>{table}"
    )
    return _page("Clinical records — ketviewer", body)


def _record_page(entry: Entry) -> str:
    record = entry.record
    assert record is not None
    urls = {
        p.index: f"/record/{entry.id}/attachment/{p.index}"
        for p in record.payloads
        if p.is_binary
    }
    body = render_body(record, attachment_urls=urls, embed_attachments=False, back_link="/")
    source = (
        f"uploaded {format_datetime(entry.received.isoformat())} — in memory only"
        if entry.is_upload
        else str(entry.path)
    )
    body += (
        f"<details><summary>Source file</summary><p class='sub'>{html.escape(source)}"
        f" — <a href='/record/{entry.id}/raw'>view raw XML</a></p></details>"
    )
    return _page(f"{record.title} — ketviewer", body)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(root: Path, port: int = 0, pattern: str = "*.ket") -> tuple[_Server, str]:
    """Start the review server on localhost. Returns (server, url)."""
    Handler.library = Library(root, pattern)
    server = _Server(("127.0.0.1", port), Handler)
    host, bound_port = server.socket.getsockname()[:2]
    return server, f"http://{host}:{bound_port}/"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

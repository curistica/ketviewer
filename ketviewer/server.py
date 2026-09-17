"""A local review server for a folder of .ket messages.

Binds to the loopback interface only and serves nothing outside the folder it
was pointed at: clinical records stay on the machine they are reviewed on.
"""

from __future__ import annotations

import html
import http.server
import socket
import socketserver
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .model import Record, format_datetime, format_nhs_number
from .parser import KetParseError, parse_file
from .render import CSS, render_body

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
form.search{margin:0 0 16px}
form.search input{width:100%;padding:10px 14px;border-radius:8px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink);font-size:15px}
.count{color:var(--muted);font-size:13px;margin:0 0 14px}
.err{color:var(--bad-ink)}
@media (max-width:640px){td.meta{white-space:normal}}
"""


@dataclass
class Entry:
    path: Path
    record: Record | None
    error: str | None = None

    @property
    def mtime(self) -> float:
        return self.path.stat().st_mtime


class Library:
    """Parsed .ket files in a folder, re-parsed when a file changes on disk."""

    def __init__(self, root: Path, pattern: str = "*.ket") -> None:
        self.root = root.resolve()
        self.pattern = pattern
        self._cache: dict[Path, tuple[float, Entry]] = {}

    def files(self) -> list[Path]:
        if self.root.is_file():
            return [self.root]
        matches = sorted(
            p for p in self.root.rglob(self.pattern) if p.is_file()
        )
        return matches

    def entries(self) -> list[Entry]:
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
            try:
                entry = Entry(path=path, record=parse_file(path))
            except KetParseError as exc:
                entry = Entry(path=path, record=None, error=str(exc))
            self._cache[path] = (mtime, entry)
            out.append(entry)
        out.sort(key=_sort_key, reverse=True)
        return out


def _sort_key(entry: Entry):
    from .model import parse_datetime

    record = entry.record
    when = None
    if record:
        when = parse_datetime(record.encounter_datetime) or parse_datetime(
            record.created_datetime
        )
    return (when is not None, when or 0, entry.path.name)


class Handler(http.server.BaseHTTPRequestHandler):
    library: Library  # set by serve()
    server_version = "ketviewer"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)
        entries = self.library.entries()

        if not parts:
            self._send_html(_index_page(entries, (query.get("q") or [""])[0]))
            return

        if parts[0] == "record" and len(parts) >= 2 and parts[1].isdigit():
            index = int(parts[1])
            if index >= len(entries):
                self._send_error_page(404, "That record is no longer in the folder.")
                return
            entry = entries[index]
            if entry.record is None:
                self._send_error_page(400, entry.error or "Unreadable file.")
                return
            if len(parts) == 2:
                self._send_html(_record_page(entry, index))
                return
            if parts[2] == "raw":
                self._send_bytes(
                    entry.path.read_bytes(), "text/plain; charset=utf-8", inline=True
                )
                return
            if parts[2] == "attachment" and len(parts) == 4 and parts[3].isdigit():
                which = int(parts[3])
                payloads = [p for p in entry.record.payloads if p.is_binary]
                match = next((p for p in payloads if p.index == which), None)
                if match is None or match.data is None:
                    self._send_error_page(404, "No such attachment.")
                    return
                self._send_bytes(match.data, match.content_type)
                return

        self._send_error_page(404, "Not found.")

    # -- plumbing --------------------------------------------------------
    def _send_html(self, body: str) -> None:
        self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", inline=True)

    def _send_bytes(self, payload: bytes, content_type: str, inline: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "object-src 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if not inline:
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_page(self, code: int, message: str) -> None:
        body = _page("ketviewer", f"<section class='card'><p class='err'>{html.escape(message)}</p>"
                     "<p><a href='/'>&larr; All records</a></p></section>")
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        return


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
    for index, entry in enumerate(entries):
        record = entry.record
        haystack = " ".join(
            filter(
                None,
                [
                    entry.path.name,
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
        if record is None:
            rows.append(
                f"<tr><td colspan='4'><span class='err'>{html.escape(entry.path.name)} — "
                f"{html.escape(entry.error or 'unreadable')}</span></td></tr>"
            )
            continue
        nhs = format_nhs_number(record.nhs_number) or "—"
        when = format_datetime(record.encounter_datetime) or format_datetime(
            record.created_datetime
        ) or "—"
        rows.append(
            f"<tr><td><a href='/record/{index}'>{html.escape(record.patient_name)}</a>"
            f"<div class='meta'>{html.escape(entry.path.name)}</div></td>"
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
        f"<p class='sub'>{html.escape(str(Handler.library.root))}</p></header>"
        "<form class='search' method='get' action='/'>"
        f"<input name='q' value='{html.escape(query)}' placeholder='Search patient, NHS number, "
        "clinician or filename' autofocus></form>"
        f"<p class='count'>{shown} of {len(entries)} record(s)</p>{table}"
    )
    return _page("Clinical records — ketviewer", body)


def _record_page(entry: Entry, index: int) -> str:
    record = entry.record
    assert record is not None
    urls = {p.index: f"/record/{index}/attachment/{p.index}" for p in record.payloads if p.is_binary}
    body = render_body(record, attachment_urls=urls, embed_attachments=False, back_link="/")
    body += (
        f"<details><summary>Source file</summary><p class='sub'>{html.escape(str(entry.path))}"
        f" — <a href='/record/{index}/raw'>view raw XML</a></p></details>"
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

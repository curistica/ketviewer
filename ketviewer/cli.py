"""Command line interface for ketviewer."""

from __future__ import annotations

import argparse
import sys
import tempfile
import webbrowser
from pathlib import Path

from . import __version__
from .model import format_datetime, format_nhs_number
from .parser import KetParseError, parse_file
from .render import render_record, render_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ketviewer",
        description="View and review .ket clinical messages. Everything stays local.",
    )
    parser.add_argument("--version", action="version", version=f"ketviewer {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_view = sub.add_parser("view", help="open one or more .ket files")
    p_view.add_argument("files", nargs="+", type=Path)
    p_view.add_argument("--text", action="store_true", help="print to the terminal instead")
    p_view.add_argument("--html", type=Path, metavar="OUT", help="write the HTML to a file")
    p_view.add_argument("--no-open", action="store_true", help="do not launch a browser")
    p_view.add_argument(
        "--save-attachments",
        type=Path,
        metavar="DIR",
        help="write any base64 attachments (PDFs etc.) to DIR",
    )

    p_serve = sub.add_parser("serve", help="review a folder of .ket files in the browser")
    p_serve.add_argument("folder", type=Path, nargs="?", default=Path.cwd())
    p_serve.add_argument("--port", type=int, default=0, help="default: a free port")
    p_serve.add_argument("--pattern", default="*.ket")
    p_serve.add_argument("--no-open", action="store_true")

    p_list = sub.add_parser("list", help="one line per record, for triage")
    p_list.add_argument("folder", type=Path, nargs="?", default=Path.cwd())
    p_list.add_argument("--pattern", default="*.ket")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "view":
        return _view(args)
    if args.command == "serve":
        return _serve(args)
    return _list(args)


def _view(args) -> int:
    status = 0
    documents: list[tuple[Path, str]] = []
    for path in args.files:
        try:
            record = parse_file(path)
        except KetParseError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            status = 1
            continue

        if args.save_attachments:
            _save_attachments(record, args.save_attachments)

        if args.text:
            print(render_text(record))
            continue
        documents.append((path, render_record(record, raw_xml=path.read_text(errors="replace"))))

    if args.text or not documents:
        return status

    if args.html:
        if len(documents) == 1:
            args.html.write_text(documents[0][1], encoding="utf-8")
            targets = [args.html]
        else:
            args.html.mkdir(parents=True, exist_ok=True)
            targets = []
            for path, doc in documents:
                out = args.html / (path.stem + ".html")
                out.write_text(doc, encoding="utf-8")
                targets.append(out)
        for target in targets:
            print(target)
    else:
        targets = []
        for path, doc in documents:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=f"-{path.stem}.html", delete=False, encoding="utf-8"
            )
            handle.write(doc)
            handle.close()
            targets.append(Path(handle.name))

    if not args.no_open:
        for target in targets:
            webbrowser.open(target.resolve().as_uri())
    return status


def _save_attachments(record, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(record.source_path or "record").stem
    for payload in record.payloads:
        if not payload.is_binary or payload.data is None:
            continue
        out = folder / f"{stem}-attachment-{payload.index + 1}{payload.extension}"
        out.write_bytes(payload.data)
        print(f"wrote {out}")


def _serve(args) -> int:
    from .server import serve

    folder = args.folder
    if not folder.exists():
        print(f"{folder}: no such folder", file=sys.stderr)
        return 1
    server, url = serve(folder, port=args.port, pattern=args.pattern)
    count = len(server.RequestHandlerClass.library.files())  # type: ignore[attr-defined]
    print(f"ketviewer serving {count} file(s) from {folder.resolve()}")
    print(f"  {url}   (localhost only — press Ctrl+C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def _list(args) -> int:
    from .server import Library

    library = Library(args.folder, args.pattern)
    entries = library.entries()
    if not entries:
        print(f"No {args.pattern} files under {args.folder}")
        return 0
    for entry in entries:
        if entry.record is None:
            print(f"{entry.path.name:<32} !! {entry.error}")
            continue
        record = entry.record
        print(
            f"{entry.path.name:<32} "
            f"{(format_datetime(record.encounter_datetime) or '—'):<22} "
            f"{record.patient_name:<28} "
            f"{(format_nhs_number(record.nhs_number) or '—'):<14} "
            f"{record.source_system or record.root_tag}"
        )
    return 0

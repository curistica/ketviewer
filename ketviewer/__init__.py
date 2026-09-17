"""ketviewer — read and review .ket clinical messages.

.ket files are XML messages (out-of-hours, 111, community services) sent to a
GP practice. This package parses them, renders the clinical content safely,
and provides a local-only viewer for reviewing a folder of records.
"""

from .model import Record
from .parser import KetParseError, parse_bytes, parse_file
from .render import render_record, render_text

__version__ = "0.1.0"
__all__ = [
    "Record",
    "KetParseError",
    "parse_file",
    "parse_bytes",
    "render_record",
    "render_text",
]

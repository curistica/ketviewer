"""Parse .ket clinical messages into a `Record`.

.ket files are XML documents produced by out-of-hours, 111 and community
systems for delivery to a GP practice.  Element names vary by sender, so the
parser is structural rather than schema-bound: every top-level child of the
root becomes a section, every leaf element a field, and anything that looks
like a clinical payload is pulled out for rendering.
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from .model import Field, Payload, Record, Section

#: Element names that carry the clinical narrative rather than metadata.
PAYLOAD_TAGS = {
    "payload",
    "report",
    "reporttext",
    "content",
    "body",
    "document",
    "attachment",
    "narrative",
    "clinicalcontent",
}

_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)
_BASE64_ONLY = re.compile(r"^[A-Za-z0-9+/=\s]+$")


class KetParseError(Exception):
    """Raised when a file cannot be read as a .ket message."""


def parse_file(path: str | Path) -> Record:
    """Parse a .ket file from disk."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:  # unreadable file, permissions, etc.
        raise KetParseError(f"Cannot read {path}: {exc}") from exc
    record = parse_bytes(raw)
    record.source_path = str(path)
    return record


def parse_bytes(raw: bytes) -> Record:
    """Parse .ket content held in memory."""
    # Refuse documents that declare their own entities: expanding them is a
    # denial-of-service risk ("billion laughs") and a .ket message has no
    # legitimate need for them.
    prolog = raw[:4096]
    doctype_end = prolog.lower().find(b"]>")
    if _ENTITY_DECL.search(prolog[: doctype_end + 2] if doctype_end != -1 else prolog):
        raise KetParseError(
            "Refusing to parse: the document declares XML entities, which is "
            "not expected in a clinical message."
        )

    text = _decode(raw)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise KetParseError(f"Not valid XML: {exc}") from exc

    record = Record(root_tag=_localname(root.tag))
    record.comments = _collect_comments(text)

    payload_index = 0
    # Fields sitting directly under the root, outside any section.
    loose = Section(tag=record.root_tag)
    for key, value in root.attrib.items():
        loose.fields.append(Field(path=f"{record.root_tag}/@{key}", value=value))

    for child in root:
        if not isinstance(child.tag, str):  # comments and processing instructions
            continue
        tag = _localname(child.tag)
        if _is_payload(tag, child):
            record.payloads.append(_build_payload(child, payload_index, record))
            payload_index += 1
            continue
        if not any(isinstance(c.tag, str) for c in child):
            # A leaf at the top level is still clinical data: keep it.
            value = (child.text or "").strip()
            if value or child.attrib:
                loose.fields.append(
                    Field(path=f"{tag}", value=value, attributes=dict(child.attrib))
                )
            continue
        section = Section(tag=tag)
        _walk(child, tag, section.fields, record)
        if section.fields or child.attrib:
            for key, value in child.attrib.items():
                section.fields.insert(0, Field(path=f"{tag}/@{key}", value=value))
            record.sections.append(section)

    if loose.fields:
        record.sections.insert(0, loose)

    # A message with a payload buried deeper than the top level still needs
    # showing; sweep the tree if we found none.
    if not record.payloads:
        for elem in root.iter():
            if isinstance(elem.tag, str) and _is_payload(_localname(elem.tag), elem):
                record.payloads.append(_build_payload(elem, payload_index, record))
                payload_index += 1

    if not record.sections and not record.payloads:
        record.warnings.append("No readable sections were found in this message.")
    return record


# --------------------------------------------------------------------------
# internals


def _decode(raw: bytes) -> str:
    """Honour the XML declaration's encoding, falling back sensibly."""
    match = re.match(rb"^\s*<\?xml[^>]*encoding=[\"']([\w.-]+)[\"']", raw[:200], re.IGNORECASE)
    encodings = []
    if match:
        encodings.append(match.group(1).decode("ascii", "ignore"))
    encodings += ["utf-8", "cp1252", "latin-1"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _localname(tag: str) -> str:
    """Strip any XML namespace: '{urn:nhs}Patient' -> 'Patient'."""
    return tag.rsplit("}", 1)[-1]


def _collect_comments(text: str) -> list[str]:
    """XML comments often carry the sender's code-list hints; keep them."""
    return [c.strip() for c in re.findall(r"<!--(.*?)-->", text, re.DOTALL) if c.strip()]


def _is_payload(tag: str, elem: ET.Element) -> bool:
    if tag.lower() in PAYLOAD_TAGS:
        return True
    lowered = {k.lower(): v for k, v in elem.attrib.items()}
    return "contenttype" in lowered or "mimetype" in lowered


def _walk(elem: ET.Element, path: str, out: list[Field], record: Record) -> None:
    """Flatten an element tree into leaf fields, preserving paths."""
    for child in elem:
        if not isinstance(child.tag, str):
            continue
        tag = _localname(child.tag)
        child_path = f"{path}/{tag}"
        grandchildren = [c for c in child if isinstance(c.tag, str)]
        if grandchildren:
            _walk(child, child_path, out, record)
            continue
        value = (child.text or "").strip()
        if value or child.attrib:
            out.append(Field(path=child_path, value=value, attributes=dict(child.attrib)))


def _build_payload(elem: ET.Element, index: int, record: Record) -> Payload:
    attrs = {k.lower(): v for k, v in elem.attrib.items()}
    content_type = attrs.get("contenttype") or attrs.get("mimetype") or "text/plain"
    encoding = (attrs.get("encoding") or "").lower()

    raw = "".join(elem.itertext())
    stripped = raw.strip()

    looks_binary = (
        encoding == "base64"
        or any(m in content_type.lower() for m in ("pdf", "octet-stream", "tiff", "image", "rtf"))
    )
    if looks_binary or (
        len(stripped) > 128 and _BASE64_ONLY.match(stripped) and "<" not in stripped
    ):
        try:
            data = base64.b64decode("".join(stripped.split()), validate=True)
            return Payload(
                content_type=content_type, data=data, attributes=dict(elem.attrib), index=index
            )
        except (binascii.Error, ValueError):
            record.warnings.append(
                f"Payload {index + 1} is declared as base64 but could not be decoded; "
                "showing it as text."
            )

    return Payload(
        content_type=content_type,
        text=_dedent(stripped),
        attributes=dict(elem.attrib),
        index=index,
    )


def _dedent(text: str) -> str:
    """CDATA blocks are usually indented to match the surrounding XML."""
    lines = text.splitlines()
    # The opening line sits directly after "<![CDATA[" and carries no indent,
    # so measure the block from the second line onwards.
    indents = [len(l) - len(l.lstrip()) for l in lines[1:] if l.strip()]
    if not indents:
        return text.strip()
    cut = min(indents)
    body = [lines[0]] + [l[cut:] if len(l) >= cut else l.lstrip() for l in lines[1:]]
    return "\n".join(body).strip()

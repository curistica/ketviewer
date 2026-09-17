"""Allowlist sanitiser for payload HTML.

Payload markup arrives from an external sending system. Rendering it verbatim
would let a message run scripts in the viewer or pull remote resources (which
would leak the fact — and timing — of a record being opened). We keep the
formatting a clinician needs and drop everything else.
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "pre", "blockquote",
    "b", "strong", "i", "em", "u", "s", "small", "sub", "sup", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
}
VOID_TAGS = {"br", "hr"}
#: Elements whose *content* is dropped as well as the tag.
DROP_CONTENT_TAGS = {"script", "style", "head", "title", "iframe", "object", "embed"}
ALLOWED_ATTRS = {
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}


class _Sanitiser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open_tags: list[str] = []
        self.suppress_depth = 0
        self.removed: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.suppress_depth:
            if tag in DROP_CONTENT_TAGS:
                self.suppress_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self.suppress_depth = 1
            self.removed.add(tag)
            return
        if tag not in ALLOWED_TAGS:
            self.removed.add(tag)
            return  # unwrap: the tag goes, its text stays
        allowed = ALLOWED_ATTRS.get(tag, set())
        rendered = "".join(
            f' {name}="{escape(value or "", quote=True)}"'
            for name, value in attrs
            if name.lower() in allowed
        )
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{rendered}>")
        else:
            self.out.append(f"<{tag}{rendered}>")
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.suppress_depth:
            return
        if tag in ALLOWED_TAGS:
            self.out.append(f"<{tag}>" if tag in VOID_TAGS else f"<{tag}></{tag}>")
        else:
            self.removed.add(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.suppress_depth:
            if tag in DROP_CONTENT_TAGS:
                self.suppress_depth -= 1
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag not in self.open_tags:
            return  # stray close tag
        # Close anything the sender left open inside this element.
        while self.open_tags:
            current = self.open_tags.pop()
            self.out.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.suppress_depth:
            return
        self.out.append(escape(data, quote=False))

    def handle_comment(self, data: str) -> None:  # comments are never rendered
        return

    def close(self) -> None:  # type: ignore[override]
        super().close()
        while self.open_tags:
            self.out.append(f"</{self.open_tags.pop()}>")

    @property
    def html(self) -> str:
        return "".join(self.out)


def sanitize_html(markup: str) -> tuple[str, set[str]]:
    """Return (safe HTML, set of element names that were removed)."""
    parser = _Sanitiser()
    parser.feed(markup)
    parser.close()
    return parser.html, parser.removed


def text_to_html(text: str) -> str:
    """Render a plain-text payload, preserving its line structure."""
    return f"<pre class='plain'>{escape(text, quote=False)}</pre>"

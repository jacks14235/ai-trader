"""Deterministic text normalization for research provider content."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")
_TAG_LIKE = re.compile(r"</?[A-Za-z][^>]*>")
_DROPPED_CONTAINERS = frozenset({"figure", "script", "style"})
_BLOCK_ELEMENTS = frozenset(
    {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)


class _PlainTextParser(HTMLParser):
    """Extract visible text while omitting non-prose and image containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._dropped_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag in _DROPPED_CONTAINERS:
            self._dropped_depth += 1
        elif not self._dropped_depth and tag in _BLOCK_ELEMENTS:
            self.parts.append(" ")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if not self._dropped_depth and tag in _BLOCK_ELEMENTS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_CONTAINERS:
            self._dropped_depth = max(0, self._dropped_depth - 1)
        elif not self._dropped_depth and tag in _BLOCK_ELEMENTS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._dropped_depth:
            self.parts.append(data)


def compact_plain_text(value: object) -> str:
    """Return collapsed visible text from a provider value that may contain HTML."""
    if value is None:
        return ""
    parser = _PlainTextParser()
    parser.feed(str(value))
    parser.close()
    # ``HTMLParser`` resolves normal references. The additional unescape handles
    # provider strings containing a doubly encoded entity such as ``&amp;amp;``.
    visible = html.unescape("".join(parser.parts))
    # A doubly encoded provider fragment can become tag-like only after the
    # second entity pass; do not let that representation leak into context.
    return _WHITESPACE.sub(" ", _TAG_LIKE.sub(" ", visible)).strip()


def alpaca_news_item_text(item: Mapping[object, object]) -> str:
    """Render one Alpaca news item as compact, model-facing plain text."""
    fields = (
        ("Headline", item.get("headline")),
        ("Source", item.get("source")),
        ("Author", item.get("author")),
        ("Published", item.get("created_at")),
        ("URL", item.get("url")),
        ("Summary", item.get("summary")),
        ("Body", item.get("content")),
    )
    return "\n".join(
        f"{label}: {text}"
        for label, value in fields
        if (text := compact_plain_text(value))
    )


def alpaca_news_text(items: Sequence[object]) -> str:
    """Render a bounded Alpaca news response without exposing provider markup."""
    rendered = (
        alpaca_news_item_text(item)
        for item in items
        if isinstance(item, Mapping)
    )
    return "\n\n---\n\n".join(text for text in rendered if text)

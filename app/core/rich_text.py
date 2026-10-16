"""Rich text from the admin's editor: what may be stored, and how it is made safe.

Product and category descriptions, the legal texts and the admin's notes are
HTML written in the panel's editor. Whatever the editor (or any other client)
sends is cleaned here before it is stored, so the storefront can put it on the
page as it is: only the tags below survive, links keep only an http(s),
mailto or tel address (or a path on this site) and get rel="noopener
noreferrer", and every attribute, script, style and comment goes.

Text written before the editor existed is plain: blank lines between
paragraphs, single line breaks inside them, and "## " starting a heading in the
legal texts. `text_to_html` turns that into the same HTML, so there is one
format to store and to show.
"""

import html
import re

import nh3

# What the editor offers: paragraphs and line breaks, bold/italic/underline,
# two heading levels (the page's <h1> is the product or category name), lists
# and links. <strong>/<em> are what the editor writes for bold and italic;
# <b>/<i> are kept for pasted text.
# Longest stored value (HTML, so a third or so above the text it holds).
RICH_TEXT_MAX = 50_000

ALLOWED_TAGS = frozenset({"p", "br", "b", "strong", "i", "em", "u", "h2", "h3", "ul", "ol", "li", "a"})
ALLOWED_ATTRIBUTES = {"a": {"href"}}
URL_SCHEMES = frozenset({"http", "https", "mailto", "tel"})

# The editor always writes block-level HTML: a value that opens with a block
# tag is HTML. Anything else is plain text — and a "<b>" typed inside it is
# shown as typed, not obeyed.
_HTML = re.compile(r"^\s*<(?:p|h[1-6]|ul|ol|div|blockquote)\b", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(?:p|h[1-6]|li|ul|ol)>|<br\s*/?>", re.IGNORECASE)


def looks_like_html(value: str) -> bool:
    return bool(_HTML.search(value))


def sanitize_html(value: str) -> str:
    """Only the allowed tags and safe links; empty paragraphs and blank edges dropped."""
    cleaned = nh3.clean(
        value,
        tags=set(ALLOWED_TAGS),
        attributes={tag: set(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()},
        url_schemes=set(URL_SCHEMES),
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
    # The editor leaves an empty paragraph behind a cleared field or a trailing Enter.
    cleaned = re.sub(r"<p>(?:\s|&nbsp;|<br\s*/?>)*</p>", "", cleaned).strip()
    return cleaned if html_to_text(cleaned) else ""


def text_to_html(value: str) -> str:
    """Plain text as written before the editor: paragraphs, line breaks, "## " headings."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", value.replace("\r\n", "\n")) if b.strip()]
    out = []
    for block in blocks:
        if block.startswith("## "):
            out.append(f"<h2>{html.escape(block[3:].strip(), quote=False)}</h2>")
        else:
            lines = [html.escape(line.strip(), quote=False) for line in block.split("\n")]
            out.append(f"<p>{'<br>'.join(lines)}</p>")
    return "".join(out)


def to_rich_html(value: str | None) -> str | None:
    """What to store for a rich-text field: cleaned HTML, or plain text made HTML.
    None when nothing readable is left."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    cleaned = sanitize_html(value) if looks_like_html(value) else sanitize_html(text_to_html(value))
    return cleaned or None


def html_to_text(value: str | None) -> str:
    """The words of a rich-text field, for a meta description, an SMS, a search."""
    if not value:
        return ""
    text = _BLOCK_END.sub(" ", value)
    text = html.unescape(_TAG.sub("", text))
    return re.sub(r"\s+", " ", text).strip()

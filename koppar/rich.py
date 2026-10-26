"""Text structure for the rich view: the assistant's Markdown and the results'
summaries, as blocks of spans. No wx here.

A block is {"kind": "p" | "h" | "li" | "row" | "code" | "gap", ...}; a span is
(text, style, link) with style normal | bold | code | muted | link | small.
"""

import re

_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def inline(text, style="normal"):
    """**bold**, `code` and [links](url) within a line."""
    spans = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            spans.append((part[2:-2], "bold", None))
        elif part.startswith("`") and part.endswith("`"):
            spans.append((part[1:-1], "code", None))
        elif part.startswith("["):
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", part)
            spans.append((m.group(1), "link", "url:" + m.group(2)) if m else (part, style, None))
        else:
            spans.append((part, style, None))
    return spans


def markdown(text):
    """The Markdown an assistant writes: paragraphs, headings, bullet and numbered
    lists, fenced code. Anything else reads as a paragraph."""
    blocks, para, code = [], [], None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if code is not None:
            if line.strip().startswith("```"):
                blocks.append({"kind": "code", "text": "\n".join(code)})
                code = None
            else:
                code.append(raw)
            continue
        if line.strip().startswith("```"):
            if para:
                blocks.append({"kind": "p", "spans": inline(" ".join(para))})
                para = []
            code = []
            continue
        m_h = re.match(r"^#{1,4}\s+(.*)$", line)
        m_li = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        m_ol = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if m_h or m_li or m_ol or not line.strip():
            if para:
                blocks.append({"kind": "p", "spans": inline(" ".join(para))})
                para = []
        if m_h:
            blocks.append({"kind": "h", "spans": inline(m_h.group(1))})
        elif m_li:
            blocks.append({"kind": "li", "spans": inline(m_li.group(2)), "level": min(2, len(m_li.group(1)) // 2)})
        elif m_ol:
            blocks.append({"kind": "li", "spans": inline(m_ol.group(3)), "level": min(2, len(m_ol.group(1)) // 2),
                           "ordinal": m_ol.group(2)})
        elif line.strip():
            para.append(line.strip())
    if code is not None:
        blocks.append({"kind": "code", "text": "\n".join(code)})
    if para:
        blocks.append({"kind": "p", "spans": inline(" ".join(para))})
    return blocks


def _cell(c, style="normal"):
    """A summary cell: text, (text, link) or a list of them. Links to nets and
    parts are set in the monospace face, as KiCad writes them."""
    if isinstance(c, tuple):
        text, link = c
        if link and link.split(":", 1)[0] in ("net", "ref"):
            return [(text, "code", link)]
        return [(text, "link" if link else style, link)]
    if isinstance(c, list):
        out = []
        for x in c:
            out.extend(_cell(x, style))
        return out
    return [(str(c), style, None)]


def summary_blocks(sections, show_all=False):
    """The result sections of summary.py as blocks; All details only when asked."""
    blocks = []
    for sec in sections:
        if sec["kind"] == "details":
            if sec["sections"]:
                blocks.append({"kind": "gap", "size": 6})
                blocks.append({"kind": "p", "spans": [("Hide the raw details" if show_all else "Show the raw details",
                                                       "link", "action:details")]})
                if show_all:
                    blocks.extend(summary_blocks(sec["sections"]))
            continue
        body = []
        if sec["kind"] == "bullets":
            for item in sec.get("items") or []:
                body.append({"kind": "li", "spans": _cell(item, "small" if sec.get("small") else "normal")})
        elif sec["kind"] == "table":
            for row in sec.get("rows") or []:
                cells = list(row)
                rest = []
                for c in cells[1:]:
                    rest.extend(_cell(c))
                    rest.append(("   ", "normal", None))
                body.append({"kind": "row", "cols": [_cell(cells[0], "muted"), rest[:-1] if rest else []]})
        elif sec["kind"] == "text" and sec.get("text"):
            body.append({"kind": "p", "spans": inline(sec["text"])})
        if body:
            blocks.append({"kind": "h", "spans": [(sec["title"], "bold", None)]})
            blocks.extend(body)
            blocks.append({"kind": "gap", "size": 10})
    return blocks

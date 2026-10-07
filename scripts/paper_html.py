"""Parse arXiv's LaTeXML HTML rendering: readable text, figures (img/svg/object), and tables.

Standard library only. Nodes keep their raw-source span so inline SVG and <table> markup can
be copied verbatim (html.parser lowercases tag/attribute names, which would break SVG).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "start", "end")

    def __init__(self, tag, attrs, parent, start):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list = []
        self.parent = parent
        self.start = start
        self.end = None

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed([c for c in n.children if isinstance(c, Node)]))

    def find_all(self, pred):
        return [n for n in self.iter() if pred(n)]

    def find(self, pred):
        return next((n for n in self.iter() if pred(n)), None)

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class _Builder(HTMLParser):
    def __init__(self, raw: str):
        super().__init__(convert_charrefs=True)
        self.raw = raw
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", raw)]
        self.root = Node("#root", {}, None, 0)
        self.stack = [self.root]

    def _pos(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self._pos()
        n = Node(tag, attrs, self.stack[-1], start)
        self.stack[-1].children.append(n)
        if tag in VOID:
            n.end = start + len(self.get_starttag_text() or "")
        else:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        start = self._pos()
        n = Node(tag, attrs, self.stack[-1], start)
        n.end = start + len(self.get_starttag_text() or "")
        self.stack[-1].children.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                pos = self._pos()
                gt = self.raw.find(">", pos)
                end = gt + 1 if gt != -1 else pos
                for n in self.stack[i:]:
                    if n.end is None:
                        n.end = end
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(raw: str) -> Node:
    b = _Builder(raw)
    b.feed(raw)
    b.close()
    for n in b.stack[1:]:
        if n.end is None:
            n.end = len(raw)
    return b.root


# ---------------------------------------------------------------- text extraction

SKIP_TAGS = {"script", "style", "noscript", "button", "nav", "header", "footer", "svg",
             "img", "object", "annotation", "annotation-xml", "input", "select", "form"}
SKIP_CLASSES = {"ltx_bibliography", "ltx_page_footer", "ltx_page_header", "ltx_authors",
                "ltx_dates", "ltx_role_affiliation", "ltx_note_outer", "ltx_rdf"}
BLOCK_TAGS = {"p", "div", "section", "article", "ul", "ol", "table", "tbody", "thead",
              "blockquote", "figcaption", "dl", "dt", "dd", "pre"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _walk(n, out: list, float_hook=None):
    if isinstance(n, str):
        out.append(re.sub(r"\s+", " ", n))
        return
    t = n.tag
    if t in SKIP_TAGS or (n.classes & SKIP_CLASSES):
        return
    if t == "math":
        tex = (n.attrs.get("alttext") or "").strip()
        if tex:
            out.append(f" $${tex}$$ " if n.attrs.get("display") == "block" else f" ${tex}$ ")
        return
    if t == "figure" and float_hook is not None:
        if float_hook(n, out):
            return
    if t in HEADINGS:
        out.append("\n\n" + "#" * HEADINGS[t] + " " + inline_text(n) + "\n\n")
        return
    if t == "li":
        out.append("\n- ")
        for c in n.children:
            _walk(c, out, float_hook)
        out.append("\n")
        return
    if t == "tr":
        out.append("\n|")
        for c in n.children:
            if isinstance(c, Node) and c.tag in ("td", "th"):
                cell: list = []
                _walk(c, cell, float_hook)
                out.append(" " + re.sub(r"\s+", " ", "".join(cell)).strip() + " |")
        out.append("\n")
        return
    if t == "br":
        out.append("\n")
        return
    block = t in BLOCK_TAGS
    if block:
        out.append("\n\n")
    for c in n.children:
        _walk(c, out, float_hook)
    if block:
        out.append("\n\n")


def tidy(text: str) -> str:
    lines = [re.sub(r"[ \t\f\v\r]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\|\n\n+\|", "|\n|", text)  # keep table rows contiguous
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def inline_text(n) -> str:
    out: list = []
    for c in n.children:
        _walk(c, out)
    return re.sub(r"\s+", " ", "".join(out)).strip()


# ---------------------------------------------------------------- figures & tables


def _top_level_floats(root: Node) -> list[Node]:
    res = []

    def rec(n):
        for c in n.children:
            if isinstance(c, Node):
                if c.tag == "figure":
                    res.append(c)
                else:
                    rec(c)
    rec(root)
    return res


def _section_of(n: Node) -> str:
    for a in n.ancestors():
        if a.tag == "section":
            h = next((c for c in a.children if isinstance(c, Node) and c.tag in HEADINGS), None)
            if h is not None:
                return inline_text(h)
    return ""


def _in_appendix(n: Node) -> bool:
    return any("ltx_appendix" in a.classes for a in n.ancestors())


def _caption(fig: Node) -> tuple[str, str]:
    cap = next((c for c in fig.children if isinstance(c, Node) and c.tag == "figcaption"), None)
    if cap is None:
        cap = fig.find(lambda x: x.tag == "figcaption")
    if cap is None:
        return "", ""
    tag = cap.find(lambda x: x.tag == "span" and "ltx_tag" in x.classes)
    label = inline_text(tag).rstrip(":. ").strip() if tag else ""
    text = inline_text(cap)
    if label and text.startswith(label):
        text = text[len(label):].lstrip(":. ").strip()
    return label, text


_SANITIZE = [
    (re.compile(r"<script\b.*?</script\s*>", re.S | re.I), ""),
    (re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I), ""),
    (re.compile(r"(href|src|xlink:href)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I), r'\1="#"'),

]
_ID_ATTR = re.compile(r'\sid="[^"]*"')


def sanitize(markup: str, strip_ids: bool = False) -> str:
    for pat, rep in _SANITIZE:
        markup = pat.sub(rep, markup)
    # ids are dropped from tables (avoid clashes in our page) but kept in SVG (clipPath refs)
    return _ID_ATTR.sub("", markup) if strip_ids else markup


def extract(raw: str, base_url: str):
    """Return (title, abstract, body_md, appendix_md, assets) for a LaTeXML page.

    assets: list of dicts with id, kind (figure|table|other), label, caption, section,
    in_appendix, images (list of {"url"} or {"svg": markup}), table_html.
    """
    from urllib.parse import urljoin

    root = parse(raw)
    article = root.find(lambda n: n.tag == "article" and "ltx_document" in n.classes)
    if article is None:
        raise ValueError("no LaTeXML <article class=ltx_document> found")

    title_n = article.find(lambda n: n.tag == "h1" and "ltx_title_document" in n.classes)
    title = inline_text(title_n) if title_n else ""
    abs_n = article.find(lambda n: "ltx_abstract" in n.classes)
    abstract = ""
    if abs_n is not None:
        parts: list = []
        for c in abs_n.children:
            if isinstance(c, Node) and c.tag in HEADINGS:
                continue
            _walk(c, parts)
        abstract = tidy("".join(parts))

    assets, by_node = [], {}
    for i, fig in enumerate(_top_level_floats(article)):
        cls = fig.classes
        kind = "table" if "ltx_table" in cls else "figure" if "ltx_figure" in cls else "other"
        if kind == "other" and fig.find(lambda x: x.tag in ("img", "object")) is not None:
            kind = "figure"
        label, caption = _caption(fig)
        aid = fig.attrs.get("id") or f"float{i + 1}"
        images = []
        for x in fig.iter():
            if x.tag == "img" and x.attrs.get("src"):
                src = x.attrs["src"]
                if src.startswith("data:") and len(src) < 3000:
                    continue  # tiny inline icon
                images.append({"url": src if src.startswith("data:") else urljoin(base_url, src)})
            elif x.tag == "object" and x.attrs.get("data"):
                images.append({"url": urljoin(base_url, x.attrs["data"])})
            elif x.tag == "svg" and not any(a.tag == "svg" for a in x.ancestors()):
                images.append({"svg": sanitize(raw[x.start:x.end])})
        table_html = None
        tbl = fig.find(lambda x: x.tag == "table" and "ltx_equation" not in x.classes
                       and "ltx_equationgroup" not in x.classes)
        if kind == "table" and tbl is not None:
            table_html = sanitize(raw[tbl.start:tbl.end], strip_ids=True)
        a = {
            "id": aid, "kind": kind, "label": label or aid, "caption": caption,
            "section": _section_of(fig), "in_appendix": _in_appendix(fig),
            "images": images, "table_html": table_html,
        }
        assets.append(a)
        by_node[id(fig)] = a

    def float_hook(n, out):
        a = by_node.get(id(n))
        if a is None:
            return False
        if a["kind"] == "figure" and a["images"]:
            out.append(f"\n\n[{a['label']} — asset `{a['id']}`] {a['caption']}\n\n")
            return True
        if a["kind"] == "table":
            out.append(f"\n\n[{a['label']} — asset `{a['id']}`] {a['caption']}\n")
            tbl_out: list = []
            for c in n.children:
                if not (isinstance(c, Node) and c.tag == "figcaption"):
                    _walk(c, tbl_out)
            out.append(tidy("".join(tbl_out)) + "\n\n")
            return True
        return False  # algorithms / text-only floats: keep their text inline

    body, appendix = [], []
    for c in article.children:
        target = appendix if isinstance(c, Node) and "ltx_appendix" in c.classes else body
        if isinstance(c, Node) and ("ltx_abstract" in c.classes or c is title_n):
            continue
        _walk(c, target, float_hook)
    return title, abstract, tidy("".join(body)), tidy("".join(appendix)), assets




_AUTH_END = ('class="ltx_abstract', 'class="ltx_dates', 'class="ltx_keywords', "<section")


def authors_text(raw: str, limit: int = 4000) -> str:
    """Plain text of the LaTeXML author block (names, affiliations, emails, author notes).
    Works on a truncated page too, since the block sits right after the title."""
    import html as _html
    i = raw.find('class="ltx_authors')
    if i == -1:
        return ""
    start = raw.find(">", i) + 1
    ends = [raw.rfind("<", i, j) for j in (raw.find(m, i) for m in _AUTH_END) if j != -1]
    block = raw[start:min(ends)] if ends else raw[start:start + 20000]
    block = re.sub(r"<annotation[^>]*>.*?</annotation>", " ", block, flags=re.S)
    block = re.sub(r"<[^>]+>", " ", block)
    return re.sub(r"\s+", " ", _html.unescape(block)).strip()[:limit]

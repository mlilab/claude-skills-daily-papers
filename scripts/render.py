#!/usr/bin/env python3
"""Render the static HTML site (or validate summaries).

  <output_dir>/index.html                       archive of all issues
  <output_dir>/assets/{style.css,app.js}
  <output_dir>/<date>/{en,ko}.html               daily index (index.html redirects by language)
  <output_dir>/<date>/papers/<id>/{en,ko}.html   paper pages

usage: render.py --date YYYY-MM-DD [--check] [--ids a,b]
       render.py --all            # re-render every issue
       render.py --archive-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import sys
import time
from pathlib import Path

from common import (SKILL_DIR, affiliation_text, issue_dir, load_config, paper_dirname,
                    load_requested, priority_hits, read_json, with_requested_candidates,
                    with_requested_selection)

LANGS = ("en", "ko")
SECTIONS = ["problem", "method", "results", "why_it_matters", "limitations", "relevance"]
REQUIRED = ["tldr", "key_points", "problem", "method", "results"]
L10N = {
    "en": {
        "tldr": "TL;DR", "key_points": "Key points", "problem": "Problem", "method": "Method",
        "results": "Results", "why_it_matters": "Why it matters", "limitations": "Limitations",
        "relevance": "Why it's on your list", "figures": "Key figures & tables",
        "abstract": "Original abstract", "orig_caption": "Original caption",
        "back": "Daily index", "archive": "Archive", "papers": "papers", "all": "All",
        "search": "Search titles, summaries, tags…", "read": "Read summary",
        "also": "Also relevant (not summarized)", "pending": "summary pending",
        "shown_of": "showing {n} of {total}",
        "src_html": "full text (HTML)", "src_pdf": "full text (PDF)",
        "src_abstract": "abstract only", "prev": "Previous issue",
        "announced": "arXiv listing announced", "generated": "Generated",
        "disclaimer": "Summaries are AI-generated from the paper text. Check the paper "
                      "before citing numbers.",
        "empty": "No papers matched your topics in this issue.",
        "issues": "issues",
    },
    "ko": {
        "tldr": "한 줄 요약", "key_points": "핵심 포인트", "problem": "문제 정의", "method": "방법",
        "results": "결과", "why_it_matters": "의의", "limitations": "한계",
        "relevance": "내 관심 주제와의 연관성", "figures": "주요 Figure & Table",
        "abstract": "원문 초록", "orig_caption": "원문 캡션",
        "back": "데일리 목록", "archive": "아카이브", "papers": "편", "all": "전체",
        "search": "제목·요약·태그 검색…", "read": "요약 보기",
        "also": "추가 관련 논문 (요약 생략)", "pending": "요약 준비 중",
        "shown_of": "전체 {total}편 중 {n}편",
        "src_html": "전문 (HTML)", "src_pdf": "전문 (PDF)", "src_abstract": "초록만",
        "prev": "이전 호", "announced": "arXiv 공개 시각", "generated": "생성",
        "disclaimer": "요약은 AI가 논문 본문을 읽고 작성했습니다. 수치를 인용하기 전에 원문을 "
                      "확인하세요.",
        "empty": "이번 호에는 관심 주제와 맞는 논문이 없습니다.",
        "issues": "호",
    },
}
WEEKDAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "ko": ["월", "화", "수", "목", "금", "토", "일"]}
# third-party assets are pinned with Subresource Integrity: a tampered CDN file will not run
FONTS = ('<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/'
         'dist/web/variable/pretendardvariable-dynamic-subset.min.css" crossorigin="anonymous" '
         'integrity="sha384-GIdEBaqGN9mNkDkMkzMHW8EKUqtpPIe/sLj1X7DIrnc9uPtLROJgmuDlh+3rBw0j">')
KATEX = """<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous"
  integrity="sha384-nB0miv6/jRmo5UMMR1wu3Gz6NLsoTkbqJghGIsx//Rlm+ZU03BU6SQNC66uf4l5+">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" crossorigin="anonymous"
  integrity="sha384-7zkQWkzuo3B5mTepMUcHkMB5jZaolc2xDwL6VFqjFALcbeS9Ggm/Yr2r3Dy4lfFg"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" crossorigin="anonymous"
  integrity="sha384-43gviWU0YVjaDtb/GhzOouOXtZMP/7XUzwPTstBeZFe/+rCMvRwr4yROQP43s0Xk"
  onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],throwOnError:false,ignoredClasses:['tbl-wrap']})"></script>"""


def feedback_head(cfg) -> str:
    """The private votes repo, read by assets/app.js. It sits inside the page HTML, which publish.py
    encrypts, so the published site never shows it in clear text."""
    fb = (cfg or {}).get("feedback") or {}
    return f'<meta name="dp-feedback" content="{esc(fb["repo"])}">' if fb.get("enabled") and fb.get("repo") else ""


def vote_html(p, date, lang) -> str:
    up, down = ("좋아요", "관심 없음") if lang == "ko" else ("Like", "Not interested")
    return (f'<span class="vote" data-key="{esc(paper_dirname(p["sel"]["id"]))}" data-date="{esc(date)}">'
            f'<button type="button" class="vote-btn" data-vote="up" title="{up}" aria-label="{up}">👍</button>'
            f'<button type="button" class="vote-btn" data-vote="down" title="{down}" aria-label="{down}">👎</button>'
            '</span>')

esc = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731


# ---------------------------------------------------------------- mini markdown


def md(text, inline=False) -> str:
    if not text:
        return ""
    if isinstance(text, list):
        return "<ul>" + "".join(f"<li>{md(x, inline=True)}</li>" for x in text) + "</ul>"
    maths = []

    def keep(m):
        maths.append(m.group(0))
        return f"\x00{len(maths) - 1}\x00"

    text = re.sub(r"\$\$.+?\$\$|\$[^$\n]+?\$", keep, str(text), flags=re.S)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    if inline:
        out = text.replace("\n", " ")
    else:
        blocks = []
        for b in re.split(r"\n\s*\n", text.strip()):
            lines = [ln.strip() for ln in b.strip().split("\n") if ln.strip()]
            if lines and all(re.match(r"^[-*•] ", ln) for ln in lines):
                blocks.append("<ul>" + "".join(f"<li>{ln[2:].strip()}</li>" for ln in lines)
                              + "</ul>")
            elif lines:
                blocks.append("<p>" + " ".join(lines) + "</p>")
        out = "\n".join(blocks)
    return re.sub(r"\x00(\d+)\x00", lambda m: html.escape(maths[int(m.group(1))], quote=False), out)


# ---------------------------------------------------------------- loading


def load_issue(cfg, date: str):
    root = issue_dir(cfg, date)
    issue = read_json(root / "issue.json", {"date": date})
    requested = load_requested(cfg, date)
    sel = with_requested_selection(read_json(root / "selected.json", {"papers": []}), requested)
    cands = {c["id"]: c for c in with_requested_candidates(
        read_json(root / "candidates.json", []), requested)}
    affil = read_json(root / "affiliations.json", {})
    # Each issue keeps the priority list it was first rendered with (.priority.json), so adding
    # an author/affiliation later applies from the next issue on, even if old issues re-render.
    snap = read_json(root / ".priority.json")
    pcfg = {**cfg, "priority": snap} if snap is not None else cfg
    votes = read_json(cfg["output_dir"] / ".feedback" / "votes.json", {})  # synced 👍/👎
    papers, others = [], []
    for s in sel.get("papers", []):
        pdir = root / "papers" / paper_dirname(s["id"])
        meta = {**(read_json(pdir / "meta.json") or cands.get(s["id"], {"id": s["id"]})),
                "selected_topics": s.get("topics", [])}
        entry = {"sel": s, "dir": pdir, "meta": meta,
                 "assets": read_json(pdir / "assets.json", {"assets": []}),
                 "summary": read_json(pdir / "summary.json"),
                 "prio": priority_hits(pcfg, meta.get("authors"), affiliation_text(
                     {**cands.get(s["id"], {}), **meta}, affil.get(s["id"]))),
                 "vote": (votes.get(paper_dirname(s["id"])) or {}).get("vote")}
        (papers if s.get("summarize", True) else others).append(entry)
    papers, others, demoted, others_total = apply_display_caps(cfg, papers, others)
    issue = {**issue, "_demoted": demoted, "_others_total": others_total, "_priority": pcfg["priority"]}
    return root, issue, papers, others


def apply_display_caps(cfg, papers, others):
    """Show at most max_papers_per_topic detailed summaries per topic (first topic; priority
    papers first, then selected.json order); extra summarized papers drop into "also relevant".
    That list shows at most max_also_relevant_per_topic papers per topic (priority papers first).
    selected.json keeps everything, so changing a cap only needs a re-render."""
    cap = cfg.get("max_papers_per_topic")
    shown, demoted = list(papers), []
    if cap:
        shown = []
        for _, group in group_by_topic(cfg, papers):
            requested = [p for p in group if p["sel"].get("requested")]
            regular = [p for p in group if not p["sel"].get("requested")]
            shown += requested + regular[:int(cap)]
            demoted += regular[int(cap):]
    pool = demoted + others
    limit = cfg.get("max_also_relevant_per_topic", cfg.get("max_also_relevant"))
    totals = {name: len(g) for name, g in group_by_topic(cfg, pool)}
    if limit is None:
        return shown, pool, demoted, totals
    picked = []
    for _, group in group_by_topic(cfg, pool):  # priority first, then demoted, then the rest
        picked += group[:int(limit)]
    return shown, picked, demoted, totals


def asset_map(p):
    return {a["id"]: a for a in p["assets"].get("assets", [])}


def hero_file(p):
    am = asset_map(p)
    for f in (p["summary"] or {}).get("figures", []):
        a = am.get(f.get("asset"))
        if a and a.get("files"):
            return a["files"][0]
    for a in p["assets"].get("assets", []):
        if a["kind"] == "figure" and a.get("files") and not a.get("in_appendix"):
            return a["files"][0]
    return None


# ---------------------------------------------------------------- validation

HANGUL = re.compile(r"[가-힣]")


def validate(p) -> list[str]:
    s, errs = p["summary"], []
    if s is None:
        return ["summary.json missing"]
    am = asset_map(p)
    for lang in LANGS:
        block = s.get(lang)
        if not isinstance(block, dict):
            errs.append(f"'{lang}' block missing")
            continue
        for k in REQUIRED:
            if not block.get(k):
                errs.append(f"{lang}.{k} missing/empty")
        if not isinstance(block.get("key_points", []), list):
            errs.append(f"{lang}.key_points must be a list")
    ko = s.get("ko") or {}
    if not ko.get("title"):
        errs.append("ko.title missing")
    ko_text = " ".join(str(ko.get(k, "")) for k in ("tldr", "problem", "method", "results"))
    # Korean keeps technical terms and names in English, so only flag text with almost no Hangul
    if ko_text and len(HANGUL.findall(ko_text)) < 0.1 * len(re.findall(r"\w", ko_text)):
        errs.append("ko fields do not look Korean (too few Hangul characters)")
    for f in s.get("figures", []):
        a = am.get(f.get("asset"))
        if a is None:
            errs.append(f"figure asset '{f.get('asset')}' not in assets.json")
        elif not (a.get("files") or a.get("table_file")):
            errs.append(f"figure asset '{f.get('asset')}' has no downloaded file")
        if not f.get("en") or not f.get("ko"):
            errs.append(f"figure '{f.get('asset')}' needs both en and ko explanations")
        if f.get("place", "figures") not in ("top", "figures", *SECTIONS):
            errs.append(f"figure '{f.get('asset')}' has unknown place '{f.get('place')}'")
    return errs


# ---------------------------------------------------------------- HTML pieces


def page(title, body, rel, lang, extra_head="", body_class=""):
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{FONTS}
<link rel="stylesheet" href="{rel}assets/style.css">
{extra_head}
<script defer src="{rel}assets/app.js"></script>
</head>
<body class="{body_class} lang-{lang}">
{body}
</body>
</html>
"""


def redirect_page(default_lang):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={default_lang}.html">
<script>try{{var l=localStorage.getItem('dp-lang');if(l==='en'||l==='ko')location.replace(l+'.html')}}catch(e){{}}</script>
</head><body><a href="ko.html">한국어</a> · <a href="en.html">English</a></body></html>
"""


def lang_switch(lang, en_href="en.html", ko_href="ko.html"):
    return (f'<nav class="lang"><a href="{en_href}" data-lang="en" class="{"on" if lang == "en" else ""}">EN</a>'
            f'<a href="{ko_href}" data-lang="ko" class="{"on" if lang == "ko" else ""}">한국어</a></nav>')


def authors_short(authors, n=6):
    authors = authors or []
    s = ", ".join(authors[:n])
    return s + (f" et al. (+{len(authors) - n})" if len(authors) > n else "")


def links_html(meta):
    L = meta.get("links", {})
    items = [("arXiv" if meta.get("arxiv_id") else "Paper", L.get("abs")), ("PDF", L.get("pdf")),
             ("HTML", L.get("html")), ("HF", L.get("hf")), ("Code", meta.get("code_url"))]
    return "".join(f'<a href="{esc(u)}" target="_blank" rel="noopener">{t}</a>' for t, u in items if u)


def prio_chip(p) -> str:
    return f'<span class="chip prio">★ {esc(" · ".join(p["prio"]))}</span>' if p.get("prio") else ""


def group_by_topic(cfg, entries):
    """[(topic, [entries])] by each paper's primary (first) topic in config order; within a
    topic: papers the reader liked (👍), then priority papers, then the rest in selected.json
    order, and papers marked 👎 last."""
    names = [t["name"] for t in cfg["topics"]]
    groups: dict[str, list] = {}
    for e in entries:
        tops = e["sel"].get("topics") or e["meta"].get("selected_topics") or ["Other"]
        groups.setdefault(tops[0], []).append(e)
    order = [n for n in names if n in groups] + [n for n in groups if n not in names]

    def rank(e):  # 👍 first, then priority, then the rest, 👎 last (stable within each)
        if e.get("vote") == "up":
            return 0
        if e.get("vote") == "down":
            return 3
        return 1 if e.get("prio") else 2
    return [(n, sorted(groups[n], key=rank)) for n in order]


def chips(topics, extra=""):
    return ('<div class="chips">' + "".join(f'<span class="chip topic">{esc(t)}</span>' for t in topics)
            + extra + "</div>")


def figure_html(fig, a, lang):
    t = L10N[lang]
    if a.get("table_file"):
        inner = f'<div class="tbl-wrap">{read_text(a["_dir"] / a["table_file"])}</div>'
    else:
        imgs = "".join(
            f'<a href="{esc(f)}" target="_blank"><img src="{esc(f)}" loading="lazy" '
            f'alt="{esc(a["label"])}"></a>' for f in a.get("files", []))
        inner = f'<div class="fig-imgs n{min(len(a.get("files", [])), 4)}">{imgs}</div>'
    explain = md(fig.get(lang) or "", inline=True)
    cap = esc(a.get("caption", ""))
    return f"""<figure class="fig">
{inner}
<figcaption><strong>{esc(a["label"])}.</strong> {explain}
<details><summary>{t["orig_caption"]}</summary><p>{cap}</p></details></figcaption>
</figure>"""


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------- paper page


def render_paper(p, lang, issue_date, cfg=None):
    t, meta, s = L10N[lang], p["meta"], p["summary"] or {}
    block = s.get(lang) or {}
    am = asset_map(p)
    for a in am.values():
        a["_dir"] = p["dir"]
    placed = {}
    for f in s.get("figures", []):
        a = am.get(f.get("asset"))
        if a and (a.get("files") or a.get("table_file")):
            placed.setdefault(f.get("place") or "figures", []).append(figure_html(f, a, lang))

    title = meta.get("title", p["sel"]["id"])
    src = p["assets"].get("source", "abstract")
    badges = [prio_chip(p)]
    if meta.get("hf_upvotes"):
        badges.append(f'<span class="chip hf">HF ▲ {meta["hf_upvotes"]}</span>')
    badges += [f'<span class="chip tag">{esc(x)}</span>' for x in s.get("tags", [])]
    meta_bits = [meta.get("id", "")]
    if meta.get("primary_category"):
        meta_bits.append(meta["primary_category"])
    if meta.get("venue"):
        meta_bits.append(meta["venue"])
    if meta.get("published"):
        meta_bits.append(str(meta["published"])[:10])
    meta_bits.append(t.get(f"src_{src}", src))

    parts = [f"""<header class="topbar"><a class="back" href="../../{lang}.html">← {t["back"]} · {issue_date}</a>
{lang_switch(lang)}</header>
<main class="wrap paper">
{chips(meta.get("selected_topics") or s.get("topics", []), "".join(badges))}
<h1>{esc(title)}</h1>"""]
    if lang == "ko" and block.get("title"):
        parts.append(f'<p class="subtitle">{esc(block["title"])}</p>')
    parts.append(f'<p class="authors">{esc(authors_short(meta.get("authors"), 12))}</p>')
    parts.append(f'<p class="meta">{" · ".join(esc(x) for x in meta_bits if x)}</p>')
    parts.append(f'<p class="links">{links_html(meta)}{vote_html(p, issue_date, lang) if s else ""}</p>')
    if not s:
        parts.append(f'<div class="pending">{t["pending"]}</div>')
    else:
        parts.append(f'<section class="tldr"><h2>{t["tldr"]}</h2>{md(block.get("tldr"))}</section>')
        if block.get("key_points"):
            parts.append(f'<section class="keypoints"><h2>{t["key_points"]}</h2>'
                         f'{md(block["key_points"])}</section>')
        parts += placed.get("top", [])
        for sec in SECTIONS:
            if block.get(sec):
                parts.append(f'<section class="sec sec-{sec}"><h2>{t[sec]}</h2>{md(block[sec])}</section>')
            parts += placed.get(sec, [])
        if placed.get("figures"):
            parts.append(f'<section class="gallery"><h2>{t["figures"]}</h2>'
                         + "\n".join(placed["figures"]) + "</section>")
    parts.append(f'<details class="abstract"><summary>{t["abstract"]}</summary>'
                 f'<p>{esc(meta.get("abstract", ""))}</p></details>')
    parts.append(f'<footer class="foot">{t["disclaimer"]}</footer></main>')
    return page(f"{title} · Daily Papers",
                "\n".join(parts), "../../../", lang, KATEX + feedback_head(cfg), "paper-page")


# ---------------------------------------------------------------- issue index


def render_issue(cfg, date, issue, papers, others, lang, prev_date):
    t = L10N[lang]
    d = dt.date.fromisoformat(date)
    topic_names = [x["name"] for x in cfg["topics"]]
    counts = {n: 0 for n in topic_names}

    def card(p):
        meta, s = p["meta"], p["summary"] or {}
        block = s.get(lang) or {}
        tops = meta.get("selected_topics") or s.get("topics") or []
        href = f"papers/{paper_dirname(p['sel']['id'])}/{lang}.html"
        hero = hero_file(p)
        thumb = (f'<a class="thumb" href="{href}"><img src="papers/{paper_dirname(p["sel"]["id"])}/'
                 f'{esc(hero)}" loading="lazy" alt=""></a>') if hero else '<div class="thumb none"></div>'
        title = meta.get("title", "")
        ko_title = block.get("title") if lang == "ko" else None
        badges = prio_chip(p)
        if meta.get("hf_upvotes"):
            badges += f'<span class="chip hf">HF ▲ {meta["hf_upvotes"]}</span>'
        if not s:
            badges += f'<span class="chip warn">{t["pending"]}</span>'
        tldr = md(block.get("tldr"), inline=True) if s else esc(meta.get("abstract", "")[:300]) + "…"
        search = " ".join([title, ko_title or "", block.get("tldr", "") if s else "",
                           " ".join(s.get("tags", [])), " ".join(meta.get("authors", [])[:20])]).lower()
        orig = f'<p class="orig-title">{esc(ko_title)}</p>' if ko_title else ""
        return (f"""<article class="card{' is-prio' if p.get('prio') else ''}" data-topics="{esc('|'.join(tops))}" data-search="{esc(search)}">
{thumb}
<div class="card-body">
{chips(tops, badges)}
<h2><a href="{href}">{esc(title)}</a></h2>
{orig}
<p class="authors">{esc(authors_short(meta.get("authors")))}</p>
<p class="card-tldr">{tldr}</p>
<p class="links"><a class="read" href="{href}">{t["read"]} →</a>{links_html(meta)}{vote_html(p, date, lang) if s else ""}</p>
</div>
</article>""")

    for p in papers:
        for x in (p["meta"].get("selected_topics") or (p["summary"] or {}).get("topics") or []):
            counts[x] = counts.get(x, 0) + 1
    sections = []
    for name, group in group_by_topic(cfg, papers):
        n_prio = sum(1 for g in group if g.get("prio"))
        sections.append(f"""<section class="topic-sec" data-topic="{esc(name)}">
<h2 class="topic-title">{esc(name)} <span class="muted">{len(group)}</span>{f' <span class="chip prio">★ {n_prio}</span>' if n_prio else ''}</h2>
<div class="cards">{"".join(card(g) for g in group)}</div>
</section>""")

    chips_bar = [f'<button class="filter on" data-topic="">{t["all"]} <b>{len(papers)}</b></button>']
    chips_bar += [f'<button class="filter" data-topic="{esc(n)}">{esc(n)} <b>{c}</b></button>'
                  for n, c in counts.items() if c]
    others_html = ""
    if others:
        blocks = []
        for name, group in group_by_topic(cfg, others):
            items = []
            for o in group:
                m = o["meta"]
                url = (m.get("links") or {}).get("abs", "#")
                items.append(f'<li data-topics="{esc("|".join(o["sel"].get("topics", [])))}">{prio_chip(o)}'
                             f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(m.get("title", o["sel"]["id"]))}</a>'
                             f'<span class="muted"> · {esc(", ".join(o["sel"].get("topics", [])))}</span>'
                             + (f'<br><span class="muted small">{esc(o["sel"].get("reason", ""))}</span>'
                                if o["sel"].get("reason") else "") + "</li>")
            tot = (issue.get("_others_total") or {}).get(name, len(group))
            cnt = t["shown_of"].format(n=len(group), total=tot) if tot > len(group) else str(len(group))
            blocks.append(f'<h3>{esc(name)} <span class="muted">{cnt}</span></h3><ul>{"".join(items)}</ul>')
        total = sum((issue.get("_others_total") or {}).values()) or len(others)
        count = (t["shown_of"].format(n=len(others), total=total) if total > len(others)
                 else str(len(others)))
        others_html = f'<section class="others"><h2>{t["also"]} · {count}</h2>{"".join(blocks)}</section>'

    stats = issue.get("stats", {})
    src = " · ".join(f"{k} {v}" for k, v in stats.items())
    announced = issue.get("announced_at", "")[:16].replace("T", " ")
    announced_label = f" · {t['announced']} {esc(announced)}" if announced else ""
    prev = (f'<a href="../{prev_date}/{lang}.html">← {t["prev"]} ({prev_date})</a>' if prev_date else "")
    body = f"""<header class="topbar"><a class="back" href="../index.html">← {t["archive"]}</a>{lang_switch(lang)}</header>
<main class="wrap issue">
<section class="masthead">
<div class="eyebrow">Daily Papers · {WEEKDAYS[lang][d.weekday()]}</div>
<h1>{date}</h1>
<p class="sub">{len(papers)} {t["papers"]}{announced_label}{f' · {esc(src)}' if src else ''}</p>
<p class="sub">{prev}</p>
</section>
<div class="toolbar">
<div class="filters">{"".join(chips_bar)}</div>
<input class="search" type="search" placeholder="{t["search"]}">
</div>
{"".join(sections) or f'<p class="empty">{t["empty"]}</p>'}
{others_html}
<footer class="foot">{t["disclaimer"]}<br>{t["generated"]} {dt.datetime.now():%Y-%m-%d %H:%M}</footer>
</main>"""
    return page(f"Daily Papers {date}", body, "../", lang, KATEX + feedback_head(cfg), "issue-page")


# ---------------------------------------------------------------- archive


def list_issues(cfg) -> list[str]:
    out = cfg["output_dir"]
    if not out.exists():
        return []
    return sorted((p.name for p in out.iterdir()
                   if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
                   and (p / "en.html").exists()), reverse=True)


def render_archive(cfg):
    rows = []
    for date in list_issues(cfg):
        root, issue, papers, others = load_issue(cfg, date)
        d = dt.date.fromisoformat(date)
        tops = {}
        for p in papers:
            for x in p["meta"].get("selected_topics", []):
                tops[x] = tops.get(x, 0) + 1
        top_titles = "".join(f"<li>{esc(p['meta'].get('title', ''))}</li>" for p in papers[:3])
        rows.append(f"""<a class="issue-row" href="{date}/index.html">
<div class="date"><b>{date}</b><span>{WEEKDAYS["ko"][d.weekday()]} · {WEEKDAYS["en"][d.weekday()]}</span></div>
<div class="what"><div class="chips">{"".join(f'<span class="chip topic">{esc(k)} {v}</span>' for k, v in tops.items())}</div>
<ul>{top_titles}</ul></div>
<div class="count"><b>{len(papers)}</b><span>papers</span></div></a>""")
    body = f"""<main class="wrap archive">
<section class="masthead"><div class="eyebrow">Daily Papers</div><h1>Archive · 아카이브</h1>
<p class="sub">{len(rows)} issues · topics: {esc(", ".join(t["name"] for t in cfg["topics"]))}</p></section>
<div class="issues">{"".join(rows) or "<p class='empty'>No issues yet.</p>"}</div>
</main>"""
    (cfg["output_dir"] / "index.html").write_text(page("Daily Papers", body, "", cfg["default_lang"]),
                                                  encoding="utf-8")


def copy_assets(cfg):
    dst = cfg["output_dir"] / "assets"
    dst.mkdir(parents=True, exist_ok=True)
    for f in ("style.css", "app.js"):
        shutil.copyfile(SKILL_DIR / "assets" / f, dst / f)


def render_issue_all(cfg, date, ids=None):
    root, issue, papers, others = load_issue(cfg, date)
    issues = sorted(set(list_issues(cfg)) | {date})
    i = issues.index(date)
    prev_date = issues[i - 1] if i > 0 else None
    problems = {}
    for p in papers:
        if ids and p["sel"]["id"] not in ids:
            continue
        errs = validate(p)
        if errs:
            problems[p["sel"]["id"]] = errs
        p["dir"].mkdir(parents=True, exist_ok=True)
        for lang in LANGS:
            (p["dir"] / f"{lang}.html").write_text(render_paper(p, lang, date, cfg), encoding="utf-8")
        (p["dir"] / "index.html").write_text(redirect_page(cfg["default_lang"]), encoding="utf-8")
    for lang in LANGS:
        (root / f"{lang}.html").write_text(render_issue(cfg, date, issue, papers, others, lang, prev_date),
                                           encoding="utf-8")
    (root / "index.html").write_text(redirect_page(cfg["default_lang"]), encoding="utf-8")
    for p in issue.get("_demoted", []):
        # summarized but over the per-topic cap: not linked, so don't leave pages to publish
        for name in ("en.html", "ko.html", "index.html"):
            (p["dir"] / name).unlink(missing_ok=True)
    if not ids and not (root / ".priority.json").exists():
        (root / ".priority.json").write_text(json.dumps(issue["_priority"], ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    if not ids:
        # completion marker read by autorun.py
        (root / ".rendered.json").write_text(json.dumps({
            "rendered_at": time.time(), "papers": len(papers), "others": len(others),
            "summarized": sum(1 for p in papers if p["summary"]),
            "problems": len(problems),
            "source_errors": sorted((issue.get("errors") or {}).keys())}), encoding="utf-8")
    return papers, others, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--ids", help="limit --check / paper rendering to these ids")
    ap.add_argument("--check", action="store_true", help="validate summaries only")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--archive-only", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    ids = set(args.ids.split(",")) if args.ids else None

    if args.check:
        if not args.date:
            sys.exit("--check needs --date")
        _, _, papers, _ = load_issue(cfg, args.date)
        report = {p["sel"]["id"]: validate(p) for p in papers if not ids or p["sel"]["id"] in ids}
        bad = {k: v for k, v in report.items() if v}
        print(json.dumps({"checked": len(report), "ok": len(report) - len(bad), "problems": bad},
                         ensure_ascii=False, indent=2))
        sys.exit(1 if bad else 0)

    copy_assets(cfg)
    dates = list_issues(cfg) if args.all else ([args.date] if args.date else [])
    summary = {}
    for date in dates:
        papers, others, problems = render_issue_all(cfg, date, ids)
        summary[date] = {"papers": len(papers), "others": len(others),
                         "summarized": sum(1 for p in papers if p["summary"]),
                         "problems": problems,
                         "index": str(issue_dir(cfg, date) / f"{cfg['default_lang']}.html")}
    render_archive(cfg)
    summary["archive"] = str(cfg["output_dir"] / "index.html")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Download full text + figures/tables for the papers in <date>/selected.json.

For each paper with "summarize": true, creates <date>/papers/<id>/ containing
  meta.json     metadata + why it was selected
  content.md    readable full text (arXiv HTML > PDF text > abstract only)
  assets.json   figures/tables with captions and local file paths
  figures/      downloaded figure images (png/jpg/svg)
  tables/       table HTML snippets

usage: fetch_papers.py --date YYYY-MM-DD [--ids id1,id2] [--force] [--workers N]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import paper_html
import paper_pdf
import svg_preview
from common import (http_get, issue_dir, load_config, load_requested, log, paper_dirname,
                    read_json, with_requested_candidates, with_requested_selection, write_json)

RASTER_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
CTYPE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
             "image/svg+xml": ".svg", "image/webp": ".webp"}
MAX_WIDTH = 1800


def _shrink(path: Path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.width <= MAX_WIDTH:
                return
            h = round(im.height * MAX_WIDTH / im.width)
            im.resize((MAX_WIDTH, h), Image.LANCZOS).save(path)
    except Exception:
        pass


FO_CHILD = re.compile(r"(<foreignObject\b[^>]*>\s*)<([A-Za-z][\w-]*)(?![^>]*\sxmlns=)")
MATH_TAG = re.compile(r"<math\b(?![^>]*\sxmlns=)")
VOID_TAG = re.compile(r"<(br|img|hr|input|wbr|col|meta|link|source)\b([^>]*?)\s*(?<!/)>", re.I)


SVG_STYLE_MARK = "daily-papers:ltx-style"
SVG_STYLE = """<style><![CDATA[/* daily-papers:ltx-style */
foreignObject { overflow: visible; }
foreignObject > .ltx_foreignobject_container { display: flex; align-items: end; height: 100%;
  width: var(--ltx-fo-width, 100%); }
.ltx_foreignobject_content { display: block; text-align: left; line-height: var(--ltx-fo-line-height, 1);
  font-family: "Latin Modern Roman", "CMU Serif", "Times New Roman", Times, "Liberation Serif",
  "Nimbus Roman", "TeX Gyre Termes", serif; font-size: 10pt; }
foreignObject[style*=font-size] { --ltx-fo-line-height: 1.2; }
foreignObject[style*=font-size] .ltx_foreignobject_content { font-size: 1em; }
.ltx_font_bold { font-weight: 700; } .ltx_font_italic { font-style: italic; }
.ltx_font_typewriter { font-family: ui-monospace, Menlo, Consolas, monospace; }
.ltx_font_sansserif { font-family: Helvetica, Arial, sans-serif; }
.ltx_font_smallcaps { font-variant: small-caps; }
.ltx_p { display: block; margin: 0; }
]]></style>"""


def fix_svg(svg: str) -> str:
    """Make an SVG cut out of an HTML page valid as a standalone file. Inside HTML, the
    children of <foreignObject> are implicitly XHTML and <math> is MathML; a standalone .svg
    needs explicit namespaces or LaTeXML's text labels silently disappear."""
    if "xmlns=" not in svg[:500]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    if "xmlns:xlink" not in svg[:800] and "xlink:" in svg:
        svg = svg.replace("<svg", '<svg xmlns:xlink="http://www.w3.org/1999/xlink"', 1)
    svg = FO_CHILD.sub(r'\1<\2 xmlns="http://www.w3.org/1999/xhtml"', svg)
    svg = MATH_TAG.sub('<math xmlns="http://www.w3.org/1998/Math/MathML"', svg)
    svg = VOID_TAG.sub(r"<\1\2/>", svg)  # HTML void elements must self-close in XML
    if "ltx_foreignobject" in svg and SVG_STYLE_MARK not in svg:
        # arXiv's page CSS normally lays out these labels; without it browsers fall back to a
        # wide default font and the labels wrap and overlap.
        svg = re.sub(r"(<svg\b[^>]*>)", lambda m: m.group(1) + SVG_STYLE, svg, count=1)
    return svg


def save_image(img: dict, stem: Path) -> str | None:
    if "svg" in img:
        out = stem.with_suffix(".svg")
        out.write_text(fix_svg(img["svg"]), encoding="utf-8")
        return out.name
    url = img["url"]
    if url.startswith("data:"):
        m = re.match(r"data:([\w/+.-]+);base64,(.*)", url, re.S)
        if not m:
            return None
        out = stem.with_suffix(CTYPE_EXT.get(m.group(1), ".png"))
        out.write_bytes(base64.b64decode(m.group(2)))
        return out.name
    r = http_get(url, timeout=60, retries=3)
    if r.status_code != 200 or not r.content:
        return None
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in RASTER_EXT | {".svg"}:
        ext = CTYPE_EXT.get(r.headers.get("Content-Type", "").split(";")[0].strip(), ".png")
    out = stem.with_suffix(ext)
    if ext == ".svg":
        out.write_text(fix_svg(r.content.decode("utf-8", "replace")), encoding="utf-8")
        return out.name
    out.write_bytes(r.content)
    if ext in RASTER_EXT:
        _shrink(out)
    return out.name


def add_preview(pdir: Path, svg: Path, entry: dict):
    """PNG rendering of an SVG figure so the summarizer can look at it (Read can't show SVG)."""
    if not svg_preview.available():
        return
    out = pdir / "previews" / (svg.stem + ".png")
    out.parent.mkdir(exist_ok=True)
    try:
        if svg_preview.render(str(svg), str(out)):
            entry.setdefault("previews", []).append(f"previews/{out.name}")
    except Exception as e:
        log(f"  preview failed for {svg.name}: {e}")


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "x"


def header_md(meta: dict, source_note: str) -> str:
    authors = meta.get("authors") or []
    a = ", ".join(authors[:12]) + (f", … (+{len(authors) - 12})" if len(authors) > 12 else "")
    lines = [f"# {meta.get('title', '')}", "",
             f"- id: {meta['id']}",
             f"- authors: {a}",
             f"- published: {meta.get('published', '')}",
             f"- categories: {', '.join(meta.get('categories') or [])}",
             f"- source of this text: {source_note}"]
    if meta.get("comment"):
        lines.append(f"- arXiv comment: {meta['comment']}")
    return "\n".join(lines)


def compose(meta, source_note, abstract, body, appendix, max_chars) -> str:
    head = header_md(meta, source_note)
    abstract = abstract or meta.get("abstract", "")
    budget = max_chars - len(head) - len(abstract)
    if len(body) > budget:
        body = body[:budget] + "\n\n[… main text truncated …]"
        appendix = ""
    else:
        room = budget - len(body)
        if appendix and len(appendix) > room:
            appendix = appendix[:max(room, 0)] + "\n\n[… appendix truncated …]"
    parts = [head, "## Abstract\n\n" + abstract, body]
    if appendix.strip():
        parts.append("# Appendix\n\n" + appendix)
    return "\n\n".join(p for p in parts if p.strip()) + "\n"


def from_html(meta, pdir, cfg):
    url = meta["links"]["html"]
    r = http_get(url, timeout=90, retries=3)
    if r.status_code != 200 or "ltx_document" not in r.text:
        return None
    r.encoding = "utf-8"
    title, abstract, body, appendix, raw_assets = paper_html.extract(r.text, r.url)
    fig_dir, tbl_dir = pdir / "figures", pdir / "tables"
    fig_dir.mkdir(exist_ok=True)
    assets, app_figs = [], 0
    for a in raw_assets:
        if a["kind"] == "other" or (a["kind"] == "figure" and not a["images"]):
            continue
        entry = {k: a[k] for k in ("id", "kind", "label", "caption", "section", "in_appendix")}
        entry["files"] = []
        if a["kind"] == "table" and a["table_html"]:
            tbl_dir.mkdir(exist_ok=True)
            tf = tbl_dir / f"{safe_name(a['id'])}.html"
            tf.write_text(a["table_html"], encoding="utf-8")
            entry["table_file"] = f"tables/{tf.name}"
        elif a["images"]:
            if a["in_appendix"]:
                app_figs += 1
                if app_figs > cfg["fetch"]["max_appendix_figures"]:
                    entry["skipped"] = "appendix figure limit"
                    assets.append(entry)
                    continue
            for k, img in enumerate(a["images"][:12], 1):
                try:
                    name = save_image(img, fig_dir / f"{safe_name(a['id'])}-{k}")
                    if name:
                        entry["files"].append(f"figures/{name}")
                        if name.endswith(".svg"):
                            add_preview(pdir, fig_dir / name, entry)
                except Exception as e:
                    log(f"  {meta['id']}: image failed {img.get('url', 'svg')[:80]}: {e}")
                time.sleep(0.1)
        assets.append(entry)
    content = compose(meta, "arXiv HTML (full text; math as LaTeX)", abstract, body, appendix,
                      cfg["fetch"]["max_content_chars"])
    return {"source": "html", "url": r.url, "content": content, "assets": assets,
            "affil": paper_html.authors_text(r.text)}


def from_pdf(meta, pdir, cfg):
    url = meta.get("links", {}).get("pdf")
    local_pdf = meta.get("local_pdf")
    if not (url or local_pdf) or not paper_pdf.available():
        return None
    pdf = pdir / "paper.pdf"
    if local_pdf:
        source = Path(local_pdf).expanduser().resolve()
        if not source.is_file():
            return None
        with source.open("rb") as f:
            if f.read(4) != b"%PDF":
                return None
        if source != pdf.resolve():
            shutil.copyfile(source, pdf)
    else:
        r = http_get(url, timeout=120, retries=3)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            return None
        pdf.write_bytes(r.content)
    text = paper_pdf.pdf_text(pdf)
    if len(text.strip()) < 500:
        return None
    body, appendix = paper_pdf.split_main_appendix(text)
    try:
        assets = paper_pdf.extract_figures(pdf, pdir / "figures")
    except Exception as e:
        log(f"  {meta['id']}: PDF figure extraction failed: {e}")
        assets = []
    content = compose(meta, "PDF text via pdftotext (layout/math may be garbled)", "", body,
                      appendix, cfg["fetch"]["max_content_chars"])
    # the title page carries the author block; good enough for affiliation matching
    return {"source": "pdf", "url": url or meta.get("links", {}).get("abs"),
            "content": content, "assets": assets,
            "affil": re.sub(r"\s+", " ", text[:2500])}


def process(paper: dict, cand: dict, root: Path, cfg, force: bool) -> dict:
    pid = paper["id"]
    pdir = root / "papers" / paper_dirname(pid)
    if (pdir / "assets.json").exists() and not force:
        a = read_json(pdir / "assets.json", {})
        meta_path = pdir / "meta.json"
        meta = read_json(meta_path) or cand
        if meta.get("selected_topics") != paper.get("topics", []) or meta.get("selection_reason") != paper.get("reason", ""):
            write_json(meta_path, {**meta, "selected_topics": paper.get("topics", []),
                                   "selection_reason": paper.get("reason", "")})
        return {"id": pid, "dir": str(pdir), "source": a.get("source"), "cached": True,
                "figures": sum(1 for x in a.get("assets", []) if x.get("files")),
                "tables": sum(1 for x in a.get("assets", []) if x.get("table_file"))}
    pdir.mkdir(parents=True, exist_ok=True)
    meta = {**cand, "selected_topics": paper.get("topics", []),
            "selection_reason": paper.get("reason", "")}
    meta.pop("score", None)
    write_json(pdir / "meta.json", meta)
    result, errors = None, []
    for fn in ((from_html, from_pdf) if cand.get("arxiv_id") else (from_pdf,)):
        try:
            result = fn(meta, pdir, cfg)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            log(f"  {pid}: {fn.__name__} failed: {e}")
            if "--debug" in __import__("sys").argv:
                traceback.print_exc()
        if result:
            break
    if not result:
        result = {"source": "abstract", "url": meta.get("links", {}).get("abs"), "assets": [],
                  "content": compose(meta, "abstract only (full text unavailable)", "", "", "",
                                     cfg["fetch"]["max_content_chars"])}
    (pdir / "content.md").write_text(result["content"], encoding="utf-8")
    write_json(pdir / "assets.json", {"source": result["source"], "url": result["url"],
                                      "assets": result["assets"], "errors": errors})
    n_fig = sum(1 for x in result["assets"] if x.get("files"))
    n_tab = sum(1 for x in result["assets"] if x.get("table_file"))
    log(f"  {pid}: {result['source']}, {n_fig} figures, {n_tab} tables, "
        f"{len(result['content'])} chars")
    return {"id": pid, "dir": str(pdir), "source": result["source"], "figures": n_fig,
            "tables": n_tab, "chars": len(result["content"]), "errors": errors,
            "_affil": {"text": result.get("affil") or "", "source": result["source"]}}


def assign_slots(cfg, root, selected: dict, cands: dict) -> dict:
    """Detailed-summary slots per topic (max_papers_per_topic, counted by each paper's first topic):
    papers by the priority authors/affiliations first; the remaining slots go to the papers most
    similar to the reader's 👍 votes (pref_score, once feedback.min_likes likes exist), otherwise
    to the papers you marked summarize: true; ties keep the order listed in selected.json.
    Returns {"promoted": [...], "demoted": [...]} and rewrites the summarize flags."""
    import probe_affiliations
    from common import affiliation_text, priority_hits
    cap = cfg.get("max_papers_per_topic")
    if not cap:
        return {}
    affil = probe_affiliations.probe_ids(cfg, root, [p["id"] for p in selected["papers"]], cands)
    pcfg = probe_affiliations.issue_cfg(cfg, root)
    groups: dict = {}
    for p in selected["papers"]:
        groups.setdefault((p.get("topics") or ["Other"])[0], []).append(p)
    changes = {"promoted": [], "demoted": []}
    for topic, group in groups.items():
        def rank(p):
            c = cands.get(p["id"], {})
            prio = priority_hits(pcfg, c.get("authors"), affiliation_text(c, affil.get(p["id"])))
            pref = c.get("pref_score")
            return (0 if prio else 1, -pref if pref is not None else 0,
                    0 if p.get("summarize", True) else 1)
        regular = sorted((p for p in group if not p.get("requested")), key=rank)
        for i, p in enumerate(regular):  # requested papers are always detailed and use no slot
            want = i < int(cap)
            if want != p.get("summarize", True):
                changes["promoted" if want else "demoted"].append(p["id"])
            p["summarize"] = want
        for p in group:
            if p.get("requested"):
                if not p.get("summarize", True):
                    changes["promoted"].append(p["id"])
                p["summarize"] = True
    return {k: v for k, v in changes.items() if v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--ids", help="comma-separated subset of selected ids")
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    root = issue_dir(cfg, args.date)
    requested = load_requested(cfg, args.date)
    original = read_json(root / "selected.json")
    selected = with_requested_selection(original, requested)
    selected.setdefault("date", args.date)
    if not selected["papers"]:
        raise SystemExit(f"{root / 'selected.json'} not found — write it after reviewing candidates.")
    cands = {c["id"]: c for c in with_requested_candidates(
        read_json(root / "candidates.json", []), requested)}
    changed = assign_slots(cfg, root, selected, cands)
    if changed or selected != original:
        write_json(root / "selected.json", selected)
        log(f"NOTE: summary slots (priority first, {cfg['max_papers_per_topic']} per topic): {changed}")
    todo = [p for p in selected["papers"] if p.get("summarize", True)]
    if args.ids:
        want = set(args.ids.split(","))
        todo = [p for p in todo if p["id"] in want]
    missing = [p["id"] for p in todo if p["id"] not in cands]
    if missing:
        log(f"WARNING: not in candidates.json (skipped): {missing}")
    todo = [p for p in todo if p["id"] in cands]
    log(f"fetching {len(todo)} papers …")
    workers = args.workers or cfg["fetch"]["workers"]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda p: process(p, cands[p["id"]], root, cfg, args.force), todo))
    affil = read_json(root / "affiliations.json", {})
    for res in results:
        new = res.pop("_affil", None)
        old = affil.get(res["id"])
        if new and new["text"] and (not old or not old.get("text")):
            affil[res["id"]] = new
    write_json(root / "affiliations.json", affil)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Find author affiliations for every paper in <date>/selected.json and report priority hits.

Only the top of each arXiv HTML page is downloaded (the author block, with affiliations and
emails, sits right after the title), so this is cheap enough to run on every selected paper,
including the ones that will not be summarized. Results go to <date>/affiliations.json:
  {id: {"text": "...author block...", "source": "html" | "none" | "openalex"}}

usage: probe_affiliations.py --date YYYY-MM-DD [--force]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor

import paper_html
from common import (affiliation_text, issue_dir, load_config, log, priority_hits, read_json,
                    session, write_json)

MAX_BYTES = 800_000


def probe(url: str) -> str | None:
    for attempt in range(3):
        try:
            r = session().get(url, stream=True, timeout=60)
        except Exception as e:
            log(f"  probe {url}: {e}")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            r.close()
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            r.close()
            return None
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if b"ltx_abstract" in buf or len(buf) > MAX_BYTES:
                break
        r.close()
        raw = buf.decode("utf-8", "replace")
        if "ltx_document" not in raw and "ltx_authors" not in raw:
            return None
        return paper_html.authors_text(raw)
    return None


def issue_cfg(cfg, root):
    """The priority list this issue uses: its snapshot if it was already rendered, else config."""
    snap = read_json(root / ".priority.json")
    return {**cfg, "priority": snap} if snap is not None else cfg


def probe_ids(cfg, root, ids, cands: dict, force=False) -> dict:
    """Fill <issue>/affiliations.json for these ids (skips ones already probed) and return it."""
    affil = read_json(root / "affiliations.json", {})
    todo = [i for i in ids if force or i not in affil or affil[i].get("source") == "none"]

    def work(pid):
        c = cands.get(pid, {})
        if c.get("arxiv_id"):
            text = probe(f"https://arxiv.org/html/{c['arxiv_id']}")
            return pid, {"text": text or "", "source": "html" if text else "none"}
        return pid, {"text": c.get("affiliation_hint") or "", "source": "openalex"}

    with ThreadPoolExecutor(max_workers=cfg["fetch"]["workers"]) as ex:
        for pid, entry in ex.map(work, todo):
            affil[pid] = entry
    write_json(root / "affiliations.json", affil)
    return affil


def mark_priority(cfg, root, cands: list, affil: dict) -> list:
    """Set cand["priority"] from authors + affiliations; return the candidates with hits."""
    pcfg = issue_cfg(cfg, root)
    hits = []
    for c in cands:
        c["priority"] = priority_hits(pcfg, c.get("authors"), affiliation_text(c, affil.get(c["id"])))
        if c["priority"]:
            hits.append(c)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--candidates", action="store_true",
                    help="probe every candidate (not only selected papers) and refresh candidates.md")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    root = issue_dir(cfg, args.date)
    cand_list = read_json(root / "candidates.json", [])
    cands = {c["id"]: c for c in cand_list}
    if args.candidates:
        ids = list(cands)
    else:
        selected = read_json(root / "selected.json")
        if not selected:
            raise SystemExit(f"{root / 'selected.json'} not found")
        ids = [p["id"] for p in selected["papers"]]
    affil = probe_ids(cfg, root, ids, cands, args.force)
    prio = mark_priority(cfg, root, cand_list, affil)
    if args.candidates:
        import fetch_candidates
        issue = read_json(root / "issue.json", {})
        write_json(root / "candidates.json", cand_list)
        fetch_candidates.write_markdown(root / "candidates.md", cfg, issue, cand_list, issue.get("stats", {}))
    sel = {p["id"]: p for p in (read_json(root / "selected.json") or {"papers": []})["papers"]}
    print(json.dumps({
        "probed_ids": len(ids),
        "no_affiliation_found": [k for k in ids if affil.get(k, {}).get("source") == "none"],
        "priority_candidates": [{"id": c["id"], "priority": c["priority"], "title": c.get("title", "")[:90],
                                 "selected": c["id"] in sel,
                                 "summarize": sel.get(c["id"], {}).get("summarize")} for c in prio],
        "note": "Priority papers that fit a topic get detailed-summary slots first "
                "(fetch_papers.py assigns slots: priority first, then your order).",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

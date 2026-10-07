#!/usr/bin/env python3
"""Add a reader-requested paper to today's issue and finish it after writing summary.json.

usage: request_paper.py prepare --arxiv 2501.01234 [--date YYYY-MM-DD]
       request_paper.py prepare --record /path/to/metadata.json [--date YYYY-MM-DD]
       request_paper.py finish --id 2501.01234 [--date YYYY-MM-DD]

The default date is the current date in the configured timezone, including weekends. A request
registry keeps these papers detailed when the routine daily selection is rewritten. The finish
step validates the summary, likes the paper in the private feedback repo, and publishes the site.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from common import (SKILL_DIR, issue_dir, load_config, load_requested, local_tz, paper_dirname,
                    read_json, requested_path, with_requested_candidates,
                    with_requested_selection, write_json)

ARXIV_ID = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")


def parse_arxiv(value: str) -> str:
    value = value.strip()
    if ARXIV_ID.fullmatch(value):
        return re.sub(r"v\d+$", "", value)
    url = urlparse(value)
    if url.scheme not in ("http", "https") or url.hostname not in ("arxiv.org", "www.arxiv.org"):
        raise ValueError("expected an arXiv ID or arxiv.org URL")
    parts = url.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] not in ("abs", "pdf", "html"):
        raise ValueError("expected an arXiv abstract, PDF or HTML URL")
    pid = re.sub(r"\.pdf$", "", parts[1])
    if not ARXIV_ID.fullmatch(pid):
        raise ValueError("invalid arXiv paper ID")
    return re.sub(r"v\d+$", "", pid)


def record_from_arg(cfg, date: str, arxiv: str | None, record_file: str | None) -> dict:
    root = issue_dir(cfg, date)
    if arxiv:
        from fetch_candidates import fetch_arxiv_ids
        pid = parse_arxiv(arxiv)
        cached = {p["id"]: p for p in with_requested_candidates(
            read_json(root / "candidates.json", []), load_requested(cfg, date))}
        rec = cached.get(pid)
        if rec is None:
            found = fetch_arxiv_ids([pid])
            rec = next((p for p in found if p["id"] == pid), None)
        if rec is None:
            raise ValueError(f"arXiv paper {pid} was not found")
    else:
        rec = json.loads(Path(record_file).read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            raise ValueError("metadata file must contain one JSON object")
    for field in ("id", "title", "abstract"):
        if not isinstance(rec.get(field), str) or not rec[field].strip():
            raise ValueError(f"paper metadata needs a nonempty {field}")
    if len(rec["id"]) > 200 or not paper_dirname(rec["id"]):
        raise ValueError("invalid paper ID")
    if not isinstance(rec.get("authors", []), list):
        raise ValueError("authors must be a list")
    if not isinstance(rec.get("sources", []), list):
        raise ValueError("sources must be a list")
    if not isinstance(rec.get("links", {}), dict):
        raise ValueError("links must be an object")
    rec = {**rec, "sources": list(dict.fromkeys((rec.get("sources") or []) + ["manual"]))}
    for key in ("abs", "pdf", "html"):
        link = (rec.get("links") or {}).get(key)
        if link and (not isinstance(link, str) or urlparse(link).scheme not in ("http", "https")):
            raise ValueError(f"links.{key} must be an HTTP(S) URL")
    if rec.get("local_pdf"):
        source = Path(rec["local_pdf"]).expanduser()
        if not source.is_absolute():
            source = (Path(record_file).resolve().parent if record_file else Path.cwd()) / source
        source = source.resolve()
        if not source.is_file():
            raise ValueError("local_pdf does not exist")
        rec["local_pdf"] = str(source)
    from fetch_candidates import score_all
    score_all(cfg, {rec["id"]: rec})
    return rec


def prepare(cfg, date: str, arxiv: str | None, record_file: str | None) -> dict:
    from fetch_candidates import write_markdown
    root = issue_dir(cfg, date)
    rec = record_from_arg(cfg, date, arxiv, record_file)
    pid = rec["id"]
    topics = [m["topic"] for m in rec["topic_matches"]] or ["Others"]
    requested = load_requested(cfg, date)
    was_requested = any(p["id"] == pid for p in requested["papers"])
    entry = {"id": pid, "topics": topics, "reason": "Requested by the reader",
             "summarize": True, "requested": True}
    requested["papers"] = [p for p in requested["papers"] if p["id"] != pid] + [entry]
    requested["candidates"][pid] = rec
    issue = read_json(root / "issue.json")
    if issue is None:
        issue = {"date": date, "timezone": str(local_tz(cfg)), "sources": ["manual"],
                 "stats": {}, "errors": {}, "manual_only": True}
    issue["sources"] = list(dict.fromkeys((issue.get("sources") or []) + ["manual"]))
    issue.setdefault("stats", {})["manual"] = len(requested["papers"])
    write_json(requested_path(cfg, date), requested)
    write_json(root / "issue.json", issue)
    candidates = with_requested_candidates(read_json(root / "candidates.json", []), requested)
    write_json(root / "candidates.json", candidates)
    selected = with_requested_selection(read_json(root / "selected.json", {"date": date, "papers": []}),
                                        requested)
    selected["date"] = date
    write_json(root / "selected.json", selected)
    write_markdown(root / "candidates.md", cfg, issue, candidates, issue.get("stats", {}))
    return {"date": date, "id": pid, "topics": topics, "already_requested": was_requested,
            "summary_exists": (root / "papers" / paper_dirname(pid) / "summary.json").exists(),
            "paper_dir": str(root / "papers" / paper_dirname(pid))}


def run_script(name: str, *args: str, timeout: int) -> dict:
    r = subprocess.run([sys.executable, str(SKILL_DIR / "scripts" / name), *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{name} failed (exit {r.returncode}): {(r.stderr or r.stdout)[-300:]}")
    return json.loads(r.stdout)


def finish(cfg, date: str, paper_id: str) -> dict:
    import feedback
    import render
    import ste_check

    pid = re.sub(r"v\d+$", "", paper_id)
    requested = load_requested(cfg, date)
    if pid not in {p["id"] for p in requested["papers"]}:
        raise ValueError(f"{pid} is not a reader-requested paper in {date}")
    if not (cfg.get("publish") or {}).get("enabled"):
        raise RuntimeError("GitHub Pages publishing is disabled")
    root, _, papers, _ = render.load_issue(cfg, date)
    paper = next((p for p in papers if p["sel"]["id"] == pid), None)
    if paper is None:
        raise RuntimeError("requested paper is missing from detailed summaries")
    problems = render.validate(paper)
    if problems:
        raise RuntimeError(f"summary validation failed: {problems}")
    ste = ste_check.check(root / "papers" / paper_dirname(pid), ste_check.load_linter())
    if ste["hard_violations"]:
        raise RuntimeError(f"English style validation failed: {ste['details']}")
    vote = feedback.vote_up(cfg, date, pid)
    synced = feedback.sync(cfg)
    if synced.get("error"):
        raise RuntimeError("feedback vote was written but local vote sync failed")
    rendered = run_script("render.py", "--date", date, timeout=600)
    if date not in rendered or pid in (rendered[date].get("problems") or {}):
        raise RuntimeError("requested summary failed after rendering")
    published = run_script("publish.py", "push", timeout=1800)
    if not published.get("pushed") and published.get("reason") != "no change":
        raise RuntimeError("GitHub Pages push did not complete")
    return {"date": date, "id": pid, "topics": paper["sel"]["topics"], "vote": vote["vote"],
            "url": (cfg["publish"].get("url") or "").rstrip("/") + f"/{date}/",
            "published": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["prepare", "finish"])
    ap.add_argument("--date", help="issue date; default is the current local date")
    inputs = ap.add_mutually_exclusive_group()
    inputs.add_argument("--arxiv", help="arXiv ID or arxiv.org URL")
    inputs.add_argument("--record", help="JSON metadata for a non-arXiv paper")
    ap.add_argument("--id", help="paper ID returned by prepare")
    args = ap.parse_args()
    cfg = load_config()
    date = args.date or dt.datetime.now(local_tz(cfg)).date().isoformat()
    dt.date.fromisoformat(date)
    if args.action == "prepare":
        if not (args.arxiv or args.record) or args.id:
            ap.error("prepare needs --arxiv or --record, and no --id")
        result = prepare(cfg, date, args.arxiv, args.record)
    else:
        if not args.id or args.arxiv or args.record:
            ap.error("finish needs --id and no --arxiv/--record")
        result = finish(cfg, date, args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

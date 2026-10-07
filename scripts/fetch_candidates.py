#!/usr/bin/env python3
"""Collect candidate papers for one issue date and pre-filter them by topic keywords.

Writes <output_dir>/<date>/candidates.json (full records) and candidates.md (compact list
for relevance review), then prints a JSON summary to stdout.

usage: fetch_candidates.py [--date YYYY-MM-DD] [--sources arxiv,huggingface,openalex]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import xml.etree.ElementTree as ET_XML

from common import (
    http_get, issue_dir, load_config, load_requested, log, priority_hits, read_json,
    resolve_issue, score_topic, with_requested_candidates, write_json,
)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
GITHUB_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def arxiv_record(pid, **kw):
    rec = {
        "id": pid,
        "arxiv_id": pid,
        "links": {
            "abs": f"https://arxiv.org/abs/{pid}",
            "pdf": f"https://arxiv.org/pdf/{pid}",
            "html": f"https://arxiv.org/html/{pid}",
        },
        "sources": [],
    }
    rec.update(kw)
    return rec


# ---------------------------------------------------------------- arXiv


def _arxiv_api(params) -> ET_XML.Element:
    # arXiv answers bursts with 429; back off generously (10, 20, ... s) before giving up
    r = http_get("https://export.arxiv.org/api/query", params=params, timeout=120,
                 retries=6, backoff=10)
    r.raise_for_status()
    return ET_XML.fromstring(r.content)


def _arxiv_entry(e) -> dict | None:
    m = ARXIV_ID_RE.search(e.findtext(f"{ATOM}id") or "")
    if not m:
        return None
    comment = clean(e.findtext(f"{ARXIV_NS}comment"))
    abstract = clean(e.findtext(f"{ATOM}summary"))
    code = GITHUB_RE.search(comment + " " + abstract)
    prim = e.find(f"{ARXIV_NS}primary_category")
    return arxiv_record(
        m.group(1),
        version=m.group(2) or "v1",
        title=clean(e.findtext(f"{ATOM}title")),
        abstract=abstract,
        authors=[clean(a.findtext(f"{ATOM}name")) for a in e.findall(f"{ATOM}author")],
        categories=[c.get("term") for c in e.findall(f"{ATOM}category")],
        primary_category=prim.get("term") if prim is not None else None,
        published=e.findtext(f"{ATOM}published"),
        comment=comment,
        code_url=code.group(0).rstrip(".") if code else None,
        sources=["arxiv"],
    )


def fetch_arxiv(cfg, issue) -> list[dict]:
    cats = list(dict.fromkeys(
        cfg["sources"]["arxiv"]["categories"]
        + [c for t in cfg["topics"] for c in t["categories"]]
    ))
    start, end = (dt.datetime.fromisoformat(x) for x in issue["arxiv_window_utc"])
    fmt = "%Y%m%d%H%M"
    query = "(" + " OR ".join(f"cat:{c}" for c in cats) + ")"
    query += f" AND submittedDate:[{start:{fmt}} TO {end:{fmt}}]"
    out, offset, page, total = [], 0, 500, None
    while total is None or offset < total:
        params = {"search_query": query, "start": offset, "max_results": page,
                  "sortBy": "submittedDate", "sortOrder": "ascending"}
        root = _arxiv_api(params)
        total = int(root.findtext(f"{OPENSEARCH}totalResults") or 0)
        entries = root.findall(f"{ATOM}entry")
        if not entries and offset < total:
            time.sleep(5)  # the API occasionally returns an empty page; retry once
            entries = _arxiv_api(params).findall(f"{ATOM}entry")
            if not entries:
                log(f"arXiv: empty page at offset {offset}/{total}; stopping early")
                break
        for e in entries:
            rec = _arxiv_entry(e)
            if rec is None:
                continue
            pub = dt.datetime.fromisoformat(rec["published"].replace("Z", "+00:00"))
            if start <= pub <= end:  # otherwise a replacement of an older paper
                out.append(rec)
        offset += len(entries)
        log(f"arXiv: {offset}/{total}")
        if offset < total:
            time.sleep(3)  # arXiv API etiquette
    return out


def fetch_arxiv_ids(ids: list[str]) -> list[dict]:
    root = _arxiv_api({"id_list": ",".join(ids), "max_results": len(ids)})
    out = [r for r in map(_arxiv_entry, root.findall(f"{ATOM}entry")) if r]
    for r in out:
        r["sources"] = ["manual"]
    return out


# ---------------------------------------------------------------- Hugging Face Daily Papers


def fetch_hf(cfg, issue) -> list[dict]:
    out = []
    for d in issue["hf_dates"]:
        r = http_get("https://huggingface.co/api/daily_papers", params={"date": d, "limit": 100})
        if r.status_code != 200:
            log(f"HF {d}: HTTP {r.status_code}")
            continue
        items = r.json()
        log(f"HF {d}: {len(items)} papers")
        for it in items:
            p = it.get("paper", {})
            pid = p.get("id")
            if not pid or not ARXIV_ID_RE.fullmatch(pid):
                continue
            out.append(arxiv_record(
                pid,
                title=clean(p.get("title") or it.get("title")),
                abstract=clean(p.get("summary") or it.get("summary")),
                authors=[clean(a.get("name")) for a in p.get("authors", [])],
                published=p.get("publishedAt"),
                hf_upvotes=p.get("upvotes", 0),
                hf_date=d,
                hf_org=" ".join(filter(None, [(it.get("organization") or p.get("organization") or {}).get(k)
                                              for k in ("name", "fullname")])) or None,
                sources=["huggingface"],
            ))
            out[-1]["links"]["hf"] = f"https://huggingface.co/papers/{pid}"
    return out


# ---------------------------------------------------------------- OpenAlex (journal/conference papers)


def _openalex_abstract(inv):
    if not inv:
        return ""
    pos = sorted((i, w) for w, idxs in inv.items() for i in idxs)
    return " ".join(w for _, w in pos)


def fetch_openalex(cfg, issue) -> list[dict]:
    lo, hi = issue["hf_dates"][0], issue["hf_dates"][-1]
    cap = int(cfg["sources"]["openalex"].get("max_results_per_topic", 40))
    out = []
    for t in cfg["topics"]:
        # OpenAlex full-text search takes plain phrases: drop regexes, unwrap title: keywords
        kws = [k[6:] if k.startswith("title:") else k for k in t["keywords"]]
        kws = [k for k in kws if not k.startswith("re:")]
        if not kws:
            continue
        search = " OR ".join(f'"{k}"' for k in kws)
        r = http_get("https://api.openalex.org/works", params={
            "search": search,
            "filter": f"from_publication_date:{lo},to_publication_date:{hi},"
                      "type:article|preprint|review",
            "per-page": min(cap, 200),
            "select": "id,doi,title,publication_date,type,authorships,abstract_inverted_index,"
                      "primary_location,best_oa_location,ids",
        })
        if r.status_code != 200:
            raise RuntimeError(f"OpenAlex [{t['name']}]: HTTP {r.status_code} {r.text[:200]}")
        res = r.json().get("results", [])
        log(f"OpenAlex [{t['name']}]: {len(res)} works")
        for w in res:
            prim = w.get("primary_location") or {}
            oa = w.get("best_oa_location") or {}
            urls = " ".join(filter(None, [w.get("doi"), prim.get("landing_page_url"),
                                          oa.get("landing_page_url"), oa.get("pdf_url")]))
            m = re.search(r"arxiv[./](?:org/abs/)?(\d{4}\.\d{4,5})", urls, re.I)
            common = dict(
                title=clean(w.get("title")),
                abstract=clean(_openalex_abstract(w.get("abstract_inverted_index"))),
                authors=[clean((a.get("author") or {}).get("display_name"))
                         for a in w.get("authorships", [])],
                published=w.get("publication_date"),
                venue=((prim.get("source") or {}).get("display_name")),
                affiliation_hint="; ".join(dict.fromkeys(
                    i.get("display_name", "") for a in w.get("authorships", [])
                    for i in a.get("institutions", []) if i.get("display_name"))) or None,
                sources=["openalex"],
            )
            if m:
                out.append(arxiv_record(m.group(1), **common))
            else:
                wid = w["id"].rsplit("/", 1)[-1]
                out.append({
                    "id": f"openalex:{wid}",
                    "links": {k: v for k, v in {
                        "abs": w.get("doi") or prim.get("landing_page_url") or w["id"],
                        "pdf": oa.get("pdf_url"),
                        "openalex": w["id"],
                    }.items() if v},
                    **common,
                })
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------- merge & score


def merge(records: list[dict]) -> dict[str, dict]:
    papers: dict[str, dict] = {}
    seen_titles = {}
    for r in records:
        if r["id"].startswith("openalex:"):
            # OpenAlex often lists the same work twice (preprint + published version)
            key = re.sub(r"\W+", " ", (r.get("title") or "").lower()).strip()
            if key in seen_titles:
                r = {**r, "id": seen_titles[key]}
            else:
                seen_titles[key] = r["id"]
        cur = papers.get(r["id"])
        if cur is None:
            papers[r["id"]] = r
            continue
        for k, v in r.items():
            if k == "sources":
                cur["sources"] = list(dict.fromkeys(cur["sources"] + v))
            elif k == "links":
                cur["links"] = {**v, **cur["links"]}
            elif not cur.get(k) and v:
                cur[k] = v
        # prefer arXiv's own metadata when present
        if "arxiv" in r["sources"]:
            for k in ("title", "abstract", "authors"):
                if r.get(k):
                    cur[k] = r[k]
    return papers


def score_all(cfg, papers: dict[str, dict]):
    min_score = cfg.get("min_score", 1)
    for p in papers.values():
        matches = []
        for t in cfg["topics"]:
            s, hits = score_topic(t, p.get("title", ""), p.get("abstract", ""), p.get("authors", []))
            if s >= t.get("min_score", min_score):
                matches.append({"topic": t["name"], "score": s, "hits": hits})
        matches.sort(key=lambda m: -m["score"])
        p["topic_matches"] = matches
        p["score"] = (matches[0]["score"] if matches else 0) + (1 if p.get("hf_upvotes") else 0)
        p["priority"] = priority_hits(cfg, p.get("authors"), " ; ".join(
            filter(None, [p.get("hf_org"), p.get("affiliation_hint")])))


def is_candidate(cfg, p) -> bool:
    if p["topic_matches"]:
        return True
    return "huggingface" in p["sources"] and cfg["sources"]["huggingface"].get("include_all")


def add_preference(cfg, cands: list) -> dict:
    """Sync 👍/👎 votes and give every candidate pref_score (similarity to liked papers)."""
    import feedback
    res = feedback.sync(cfg)
    scorer = feedback.scorer_for(cfg, cands)
    for c in cands:
        if scorer:
            c["pref_score"] = scorer.score(c)
        else:
            c.pop("pref_score", None)
    return {**res, "ranking_active": bool(scorer)}


def write_markdown(path, cfg, issue, cands, stats):
    L = [f"# Candidates — issue {issue['date']}", ""]
    if issue.get("arxiv_window_utc"):
        L.append(f"arXiv window (UTC): {issue['arxiv_window_utc'][0]} → {issue['arxiv_window_utc'][1]}  ")
    if issue.get("hf_dates"):
        L.append(f"HF/OpenAlex dates: {', '.join(issue['hf_dates'])}  ")
    L.append(f"Fetched: {json.dumps(stats)}")
    L += ["", "## Topics", ""]
    for t in cfg["topics"]:
        L.append(f"- **{t['name']}** — {t['description'].strip()}")
    prio = [p for p in cands if p.get("priority")]
    if prio:
        L += ["", f"## ★ Priority candidates ({len(prio)})", "",
              "Papers by the priority authors/affiliations in the config. Select every one that fits a "
              "topic (skip only keyword collisions); they get detailed-summary slots first.", ""]
        for p in prio:
            tm = ", ".join(m["topic"] for m in p["topic_matches"]) or "no keyword hit"
            L.append(f"- `{p['id']}` — {p.get('title', '')}  ·  ★ {', '.join(p['priority'])}  ·  {tm}")
    try:
        import feedback
        votes = feedback.load_dataset(cfg)
    except Exception:
        votes = []
    if votes:
        recent = sorted(votes, key=lambda v: v["date"], reverse=True)[:15]
        L += ["", f"## Your recent votes ({len(votes)} total)", "",
              "What the reader liked (👍) or did not want (👎) in earlier digests. Candidates also show "
              "♥ = similarity to the liked papers when enough votes exist.", ""]
        L += [f"- {'👍' if v['vote'] == 'up' else '👎'} {v.get('title') or v['key']}" for v in recent]
    L += ["", f"## Candidates ({len(cands)}, sorted by keyword score)", ""]
    for i, p in enumerate(cands, 1):
        tm = "; ".join(f"{m['topic']} [{', '.join(m['hits'])}]" for m in p["topic_matches"]) \
            or "no keyword hit"
        extra = []
        if p.get("hf_upvotes") is not None and "huggingface" in p["sources"]:
            extra.append(f"HF▲{p['hf_upvotes']}")
        if p.get("venue"):
            extra.append(p["venue"])
        if p.get("hf_org"):
            extra.append(f"HF org: {p['hf_org']}")
        if p.get("priority"):
            extra.append("★ priority: " + ", ".join(p["priority"]))
        if p.get("pref_score") is not None:
            extra.append(f"♥ {p['pref_score']:.2f}")
        if p.get("primary_category"):
            extra.append(p["primary_category"])
        L.append(f"### {i}. `{p['id']}` — {p.get('title', '')}")
        L.append(f"score {p['score']} · {tm} · src: {','.join(p['sources'])}"
                 + (f" · {' · '.join(extra)}" if extra else ""))
        ab = p.get("abstract", "")
        n = 650 if p["topic_matches"] else 350
        L.append("")
        L.append(ab[:n] + ("…" if len(ab) > n else ""))
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def add_ids(cfg, issue, ids):
    out = issue_dir(cfg, issue["date"])
    cands = read_json(out / "candidates.json", [])
    recs = fetch_arxiv_ids(ids)
    papers = merge(recs)
    score_all(cfg, papers)
    have = {c["id"]: c for c in cands}
    new = []
    for pid, rec in papers.items():
        if pid in have:
            have[pid]["sources"] = list(dict.fromkeys(have[pid]["sources"] + ["manual"]))
        else:
            new.append(rec)
    cands = new + cands
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "issue.json").exists():
        write_json(out / "issue.json", {**issue, "sources": ["manual"], "stats": {}, "errors": {}})
    write_json(out / "candidates.json", cands)
    stats = read_json(out / "issue.json", {}).get("stats", {})
    write_markdown(out / "candidates.md", cfg, issue, cands, stats)
    print(json.dumps({"issue_date": issue["date"], "added": sorted(papers),
                      "not_found": sorted(set(ids) - set(papers)),
                      "note": "Now add them to selected.json and run fetch_papers.py --ids …"},
                     ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="issue date in your timezone (default: latest published)")
    ap.add_argument("--sources", help="comma list overriding config, e.g. arxiv,huggingface")
    ap.add_argument("--max-candidates", type=int)
    ap.add_argument("--add-ids", help="append specific arXiv ids to an existing issue's candidates")
    ap.add_argument("--no-probe", action="store_true", help="skip the affiliation probe of candidates")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg["topics"]:
        raise SystemExit("No topics registered in config.yaml — add at least one topic first.")
    issue = resolve_issue(cfg, args.date)
    if args.add_ids:
        return add_ids(cfg, issue, [re.sub(r"v\d+$", "", x.strip()) for x in args.add_ids.split(",")])
    for n in issue["notes"]:
        log("NOTE:", n)
    enabled = [s for s, c in cfg["sources"].items() if c.get("enabled")]
    if args.sources:
        enabled = args.sources.split(",")

    fetchers = {"arxiv": fetch_arxiv, "huggingface": fetch_hf, "openalex": fetch_openalex}
    records, stats, errors = [], {}, {}
    for name in enabled:
        try:
            got = fetchers[name](cfg, issue)
            stats[name] = len(got)
            records += got
        except Exception as e:  # keep going with the other sources
            errors[name] = str(e)
            log(f"{name} failed: {e}")

    papers = merge(records)
    score_all(cfg, papers)
    # Keyword-matched papers are capped by score; HF Daily papers without a keyword hit
    # (include_all) are listed after them uncapped so they never crowd out real matches.
    matched = [p for p in papers.values() if p["topic_matches"]]
    # Each topic keeps its own best matches, so a broad topic (e.g. "agentic") cannot crowd the
    # others out of the review list; then an overall safety cap.
    per_topic = int(cfg.get("max_candidates_per_topic") or 0)
    dropped_by_topic = {}
    if per_topic:
        keep = set()
        for t in cfg["topics"]:
            ranked = sorted((p for p in matched if any(m["topic"] == t["name"] for m in p["topic_matches"])),
                            key=lambda p: (-next(m["score"] for m in p["topic_matches"] if m["topic"] == t["name"]),
                                           -(p.get("hf_upvotes") or 0)))
            keep.update(p["id"] for p in ranked[:per_topic])
            if len(ranked) > per_topic:
                dropped_by_topic[t["name"]] = len(ranked) - per_topic
        matched = [p for p in matched if p["id"] in keep]
    matched.sort(key=lambda p: (-p["score"], -(p.get("hf_upvotes") or 0)))
    limit = args.max_candidates or cfg["max_candidates"]
    truncated = max(0, len(matched) - limit)
    extra = [p for p in papers.values() if not p["topic_matches"] and is_candidate(cfg, p)]
    extra.sort(key=lambda p: -(p.get("hf_upvotes") or 0))
    cands = with_requested_candidates(matched[:limit] + extra, load_requested(cfg, issue["date"]))

    out = issue_dir(cfg, issue["date"])
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "issue.json", {**issue, "sources": enabled, "stats": stats, "errors": errors})
    if not args.no_probe and cands:
        # affiliations of every candidate, so selection already knows the priority papers
        import probe_affiliations
        affil = probe_affiliations.probe_ids(cfg, out, [c["id"] for c in cands], {c["id"]: c for c in cands})
        probe_affiliations.mark_priority(cfg, out, cands, affil)
    pref = add_preference(cfg, cands)
    write_json(out / "candidates.json", cands)
    write_markdown(out / "candidates.md", cfg, issue, cands, stats)

    print(json.dumps({
        "issue_date": issue["date"],
        "issue_dir": str(out),
        "notes": issue["notes"],
        "fetched": stats,
        "errors": errors,
        "unique_papers": len(papers),
        "candidates": len(cands),
        "keyword_matched": len(matched),
        "hf_without_keyword_hit": len(extra),
        "dropped_over_limit": truncated,
        "dropped_per_topic_cap": dropped_by_topic,
        "priority_candidates": sum(1 for c in cands if c.get("priority")),
        "preference": pref,
        "candidates_md": str(out / "candidates.md"),
        "selected_json_to_write": str(out / "selected.json"),
    }, ensure_ascii=False, indent=2))
    if errors:
        # files are written, but a source is missing: exit 2 so callers notice and retry
        log("ERROR: some sources failed; wait a few minutes and re-run this command.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()

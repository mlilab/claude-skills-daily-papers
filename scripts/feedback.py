#!/usr/bin/env python3
"""👍 / 👎 votes from the digest pages, and a preference score for new candidates.

The pages write one small file per vote into the private repo `feedback.repo`, using a
fine-grained token that lives only in the reader's browser:
    votes/<issue date>__<paper key>.up.json      (or .down.json)
    content: {"id": <paper key>, "date": ..., "vote": "up"|"down", "at": <time>}
The server reads that folder with its own token (publish.token_file). A reader-requested
paper can also be marked 👍 from this server. The token never appears in a published page.

usage: feedback.py sync     # votes → <output_dir>/.feedback/{votes.json,dataset.jsonl}
       feedback.py status   # counts and whether the preference ranking is active

Preference score (used to fill the summary slots left after priority papers, once at least
`feedback.min_likes` papers are liked): TF-IDF over title + abstract (+ tags), the mean cosine
similarity to the 3 most similar liked papers minus half of the same for disliked papers.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

from common import issue_dir, load_config, paper_dirname, read_json, session, write_json

NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})__([A-Za-z0-9._-]+)\.(up|down)\.json$")
STOP = set("""a an the and or of to in on for with by from as at is are was were be been being this that
these those it its we our us they their them which who whom whose what when where why how than then
there here such into over under via using use used based can could may might will would should also
not no nor but if while both each other more most less few many much very new show shows shown
paper propose proposed present approach method methods results result model models task tasks data
dataset large language llm llms study work works however existing across without within between
two three one first second further number same different including include includes achieve
achieves improve improves improved improvement performance""".split())


def fb_cfg(cfg) -> dict:
    return cfg.get("feedback") or {}


def fb_dir(cfg) -> Path:
    return cfg["output_dir"] / ".feedback"


def vote_up(cfg, date: str, paper_id: str) -> dict:
    """Give a requested paper one 👍 in the private feedback repo, replacing any older vote."""
    fb = fb_cfg(cfg)
    if not fb.get("enabled") or not fb.get("repo"):
        raise RuntimeError("feedback repo is not configured")
    tf = Path(os.path.expanduser(fb.get("token_file") or cfg["publish"]["token_file"]))
    if not tf.exists():
        raise RuntimeError("feedback repository token is missing")
    headers = {"Authorization": f"token {tf.read_text().strip()}",
               "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{fb['repo']}/contents/votes"
    r = session().get(base, headers=headers, timeout=30)
    if r.status_code == 404:
        existing = []
    elif r.status_code == 200 and isinstance(r.json(), list):
        existing = r.json()
    else:
        raise RuntimeError(f"feedback vote list: GitHub HTTP {r.status_code}")
    key = paper_dirname(paper_id)
    desired = f"{date}__{key}.up.json"
    same = next((f for f in existing if f.get("name") == desired), None)
    if same is None:
        content = {"id": key, "date": date, "vote": "up",
                   "at": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "manual_request"}
        payload = {"message": "like reader-requested paper",
                   "content": base64.b64encode(json.dumps(content).encode()).decode()}
        put = session().put(base + "/" + desired, headers=headers, json=payload, timeout=30)
        if put.status_code not in (200, 201):
            raise RuntimeError(f"feedback vote write: GitHub HTTP {put.status_code}")
    removed = 0
    for f in existing:
        m = NAME_RE.fullmatch(f.get("name", ""))
        if not m or m.group(2) != key or f["name"] == desired:
            continue
        delete = session().delete(base + "/" + f["name"], headers=headers,
                                  json={"message": "replace vote for reader-requested paper",
                                        "sha": f["sha"]}, timeout=30)
        if delete.status_code not in (200, 404):
            raise RuntimeError(f"feedback old vote removal: GitHub HTTP {delete.status_code}")
        removed += 1
    return {"vote": "up", "created": same is None, "old_votes_removed": removed}


def sync(cfg) -> dict:
    fb = fb_cfg(cfg)
    if not fb.get("enabled") or not fb.get("repo"):
        return {"skipped": "feedback disabled"}
    tf = Path(os.path.expanduser(fb.get("token_file") or cfg["publish"]["token_file"]))
    if not tf.exists():
        return {"error": f"no server token at {tf}"}
    headers = {"Authorization": f"token {tf.read_text().strip()}", "Accept": "application/vnd.github+json"}
    r = session().get(f"https://api.github.com/repos/{fb['repo']}/contents/votes", headers=headers,
                      params={"per_page": 1000}, timeout=30)
    if r.status_code == 404:
        items = []
    elif r.status_code != 200:
        return {"error": f"GitHub HTTP {r.status_code}"}
    else:
        items = r.json()
    votes = {}
    for it in items:
        m = NAME_RE.match(it.get("name", ""))
        if m:
            date, key, vote = m.groups()
            votes[key] = {"key": key, "date": date, "vote": vote}
    dataset, cand_cache = [], {}
    for v in votes.values():
        pdir = issue_dir(cfg, v["date"]) / "papers" / v["key"]
        if v["date"] not in cand_cache:
            cand_cache[v["date"]] = {paper_dirname(c["id"]): c for c in
                                     read_json(issue_dir(cfg, v["date"]) / "candidates.json", [])}
        meta = read_json(pdir / "meta.json") or cand_cache[v["date"]].get(v["key"], {})
        summ = read_json(pdir / "summary.json") or {}
        dataset.append({**v, "id": meta.get("id", v["key"]), "title": meta.get("title", ""),
                        "abstract": meta.get("abstract", ""), "tags": summ.get("tags", []),
                        "topics": meta.get("selected_topics", [])})
    d = fb_dir(cfg)
    write_json(d / "votes.json", votes)
    with open(d / "dataset.jsonl", "w", encoding="utf-8") as f:
        for row in sorted(dataset, key=lambda x: (x["date"], x["key"])):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    up = sum(1 for v in votes.values() if v["vote"] == "up")
    return {"votes": len(votes), "likes": up, "dislikes": len(votes) - up}


def load_dataset(cfg) -> list[dict]:
    p = fb_dir(cfg) / "dataset.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------- TF-IDF preference score


def _terms(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z][a-z0-9\-]+", text.lower()) if len(w) > 2 and w not in STOP]
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]


def _doc(p: dict) -> str:
    return " ".join([p.get("title", "")] * 2 + [p.get("abstract", ""), " ".join(p.get("tags", []))])


class Scorer:
    def __init__(self, liked: list[dict], disliked: list[dict], corpus: list[dict]):
        docs = [_terms(_doc(x)) for x in liked + disliked + corpus]
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self.liked = [self.vec(_doc(x)) for x in liked]
        self.disliked = [self.vec(_doc(x)) for x in disliked]

    def vec(self, text: str) -> dict:
        tf = Counter(_terms(text))
        v = {t: c * self.idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    @staticmethod
    def _top(v, refs, k=3) -> float:
        if not refs:
            return 0.0
        sims = sorted((sum(w * r.get(t, 0.0) for t, w in v.items()) for r in refs), reverse=True)[:k]
        return sum(sims) / len(sims)

    def score(self, paper: dict) -> float:
        v = self.vec(_doc(paper))
        return round(self._top(v, self.liked) - 0.5 * self._top(v, self.disliked), 4)


def scorer_for(cfg, corpus: list[dict]) -> Scorer | None:
    """A Scorer once enough papers are liked, else None (ranking falls back to Claude's marks)."""
    fb = fb_cfg(cfg)
    if not fb.get("enabled"):
        return None
    data = load_dataset(cfg)
    liked = [x for x in data if x["vote"] == "up"]
    if len(liked) < int(fb.get("min_likes", 3)):
        return None
    return Scorer(liked, [x for x in data if x["vote"] == "down"], corpus)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("sync", "status"):
        sys.exit(__doc__)
    cfg = load_config()
    if sys.argv[1] == "sync":
        res = sync(cfg)
    else:
        data = load_dataset(cfg)
        likes = sum(1 for x in data if x["vote"] == "up")
        res = {"votes": len(data), "likes": likes, "dislikes": len(data) - likes,
               "ranking_active": likes >= int(fb_cfg(cfg).get("min_likes", 3))}
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

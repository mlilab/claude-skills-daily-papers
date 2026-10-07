"""Shared helpers for the daily-papers skill: config, issue dates, HTTP, keyword matching."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
    import yaml
except ImportError:
    # `python3` may resolve to an environment without these (e.g. a conda base env); fall back
    # to the system interpreter, which has requests/pyyaml/Pillow/cryptography
    if os.path.realpath(sys.executable) != os.path.realpath("/usr/bin/python3") \
            and os.path.exists("/usr/bin/python3"):
        os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv])
    sys.exit("daily-papers needs the Python packages requests and pyyaml (plus Pillow and "
             "cryptography for publishing): python3 -m pip install -r "
             f"{os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'requirements.txt')}")

SKILL_DIR = Path(__file__).resolve().parent.parent
USER_CONFIG = Path.home() / ".config" / "daily-papers" / "config.yaml"


def config_path() -> Path:
    """$DAILY_PAPERS_CONFIG, else ~/.config/daily-papers/config.yaml (written by onboarding),
    else a legacy config.yaml next to SKILL.md. User data never lives inside the skill folder,
    so the skill can be updated or shared without carrying anyone's settings."""
    env = os.environ.get("DAILY_PAPERS_CONFIG")
    if env:
        return Path(env)
    if not USER_CONFIG.exists() and (SKILL_DIR / "config.yaml").exists():
        return SKILL_DIR / "config.yaml"
    return USER_CONFIG


def state_path() -> Path:
    """Installer state (claude path, schedule start date, GitHub account) next to the config."""
    return config_path().parent / "state.json"


class NotConfigured(SystemExit):
    pass
ET = ZoneInfo("America/New_York")
USER_AGENT = "daily-papers-skill/1.0 (personal research digest; python-requests)"

# arXiv announces new submissions at 20:00 ET, Sunday through Thursday.
# (weekday(): Mon=0 ... Sun=6)
ANNOUNCE_WEEKDAYS = {6, 0, 1, 2, 3}
ANNOUNCE_TIME = dt.time(20, 0)
CUTOFF_TIME = dt.time(14, 0)


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config

DEFAULTS = {
    "timezone": None,
    "output_dir": "~/daily-papers",
    "default_lang": "ko",
    "max_candidates": 400,          # overall safety cap on candidates for review
    "max_candidates_per_topic": 60,  # top keyword matches kept per topic (fair across topics)
    "max_papers_per_topic": 10,      # detailed summaries per topic (by each paper's first topic)
    "max_also_relevant_per_topic": 30,  # "also relevant" papers shown per topic on the index
    "min_score": 1,
    "sources": {
        "arxiv": {"enabled": True, "categories": ["cs.LG", "cs.CL", "cs.CV", "cs.AI"]},
        "huggingface": {"enabled": True, "include_all": True},
        "openalex": {"enabled": True, "max_results_per_topic": 40},
    },
    "fetch": {"max_appendix_figures": 8, "max_content_chars": 140000, "workers": 3},
    "serve": {"host": "127.0.0.1", "port": 8000},
    "priority": {"affiliations": {}, "authors": []},
    "schedule": {"enabled": False, "days": "daily", "times": ["10:30"], "timezone": "Asia/Seoul"},
    "publish": {"enabled": False, "branch": "main", "repo": "papers", "figure_days": 30},
    "feedback": {"enabled": False, "repo": None, "min_likes": 3},
    "notify": {"slack": {"enabled": False}},
    "topics": [],
}


def _merge(base, over):
    if not isinstance(base, dict) or not isinstance(over, dict):
        return over if over is not None else base
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base.get(k), v) if k in base else v
    return out


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        raise NotConfigured(
            f"daily-papers is not set up yet (no config at {path}). Run the onboarding first: "
            f"python3 {SKILL_DIR / 'scripts' / 'setup.py'} status")
    with open(path, encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user)
    cfg["output_dir"] = Path(os.path.expanduser(str(cfg["output_dir"])))
    for t in cfg["topics"]:
        t.setdefault("keywords", [])
        t.setdefault("exclude", [])
        t.setdefault("authors", [])
        t.setdefault("categories", [])
        t.setdefault("description", "")
    pc = cfg["publish"]
    if pc.get("github_user"):
        # everything else about the Pages repo follows from the account and repo name
        user, repo = pc["github_user"], pc.get("repo") or "papers"
        state = read_json(state_path(), {}).get("github", {})
        pc.setdefault("remote", f"https://github.com/{user}/{repo}.git")
        pc.setdefault("url", state.get("pages_url") or f"https://{user.lower()}.github.io/{repo}/")
        pc.setdefault("git_email", state.get("email") or f"{user}@users.noreply.github.com")
    pc.setdefault("repo_dir", str(cfg["output_dir"] / ".publish" / "repo"))
    pc.setdefault("token_file", str(cfg["output_dir"] / ".publish" / "github-token"))
    return cfg


def local_tz(cfg) -> dt.tzinfo:
    if cfg.get("timezone"):
        return ZoneInfo(cfg["timezone"])
    return dt.datetime.now().astimezone().tzinfo


def issue_dir(cfg, date: str) -> Path:
    return cfg["output_dir"] / date


def paper_dirname(pid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", pid)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def requested_path(cfg, date: str) -> Path:
    """Reader-requested papers survive automatic selection rewrites for the same issue."""
    return issue_dir(cfg, date) / ".requested.json"


def load_requested(cfg, date: str) -> dict:
    return read_json(requested_path(cfg, date), {"date": date, "papers": [], "candidates": {}})


def with_requested_selection(selected: dict | None, requested: dict) -> dict:
    """Keep each requested paper detailed, regardless of what the automatic selector wrote."""
    selected = {**(selected or {}), "papers": list((selected or {}).get("papers") or [])}
    index = {p["id"]: i for i, p in enumerate(selected["papers"]) if p.get("id")}
    for req in requested.get("papers") or []:
        entry = {**req, "requested": True, "summarize": True}
        if entry["id"] in index:
            selected["papers"][index[entry["id"]]] = {**selected["papers"][index[entry["id"]]], **entry}
        else:
            index[entry["id"]] = len(selected["papers"])
            selected["papers"].append(entry)
    return selected


def with_requested_candidates(candidates: list[dict], requested: dict) -> list[dict]:
    """Merge saved manual metadata after a routine daily candidate refresh."""
    out = list(candidates)
    index = {p["id"]: i for i, p in enumerate(out)}
    for pid, rec in (requested.get("candidates") or {}).items():
        if pid in index:
            old = out[index[pid]]
            out[index[pid]] = {**old, **rec,
                               "sources": list(dict.fromkeys((old.get("sources") or []) + ["manual"]))}
        else:
            out.append(rec)
    return out


# ---------------------------------------------------------------- issue dates
#
# An "issue" for local date D is the arXiv new-submission listing that becomes
# visible on D in the user's timezone (e.g. 09:00/10:00 KST for Korea), plus the
# Hugging Face Daily Papers lists of the ET days since the previous announcement.


def _announcement_on_local_date(d: dt.date, tz) -> dt.date | None:
    """ET date of the arXiv announcement that is published on local date d, if any."""
    for k in (-1, 0, 1):
        a = d + dt.timedelta(days=k)
        if a.weekday() in ANNOUNCE_WEEKDAYS:
            inst = dt.datetime.combine(a, ANNOUNCE_TIME, ET)
            if inst.astimezone(tz).date() == d:
                return a
    return None


def _prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _prev_announcement(a: dt.date) -> dt.date:
    a -= dt.timedelta(days=1)
    while a.weekday() not in ANNOUNCE_WEEKDAYS:
        a -= dt.timedelta(days=1)
    return a


def resolve_issue(cfg, date_str: str | None) -> dict:
    """Return the issue window for local date `date_str` (default: latest published issue)."""
    tz = local_tz(cfg)
    now = dt.datetime.now(tz)
    notes = []
    if date_str:
        d = dt.date.fromisoformat(date_str)
        a = _announcement_on_local_date(d, tz)
        if a is None:
            raise SystemExit(
                f"No arXiv announcement is published on {d} ({d.strftime('%A')}) in {tz}. "
                "arXiv announces Sun-Thu 20:00 ET; pick a weekday issue date."
            )
    else:
        d = now.date()
        while True:
            a = _announcement_on_local_date(d, tz)
            if a is not None and dt.datetime.combine(a, ANNOUNCE_TIME, ET) <= now:
                break
            d -= dt.timedelta(days=1)
        if d != now.date():
            notes.append(
                f"Today's ({now.date()}) arXiv listing is not out yet or there is none; "
                f"using the latest published issue {d}."
            )
    announce = dt.datetime.combine(a, ANNOUNCE_TIME, ET)
    deadline_day = a - dt.timedelta(days=2) if a.weekday() == 6 else a  # Sunday -> Friday
    start = dt.datetime.combine(_prev_business_day(deadline_day), CUTOFF_TIME, ET)
    end = dt.datetime.combine(deadline_day, CUTOFF_TIME, ET)
    prev_a = _prev_announcement(a)
    hf_dates = []
    x = prev_a + dt.timedelta(days=1)
    while x <= a:
        hf_dates.append(x.isoformat())
        x += dt.timedelta(days=1)
    if announce > now:
        notes.append(
            f"This listing is announced at {announce.astimezone(tz):%Y-%m-%d %H:%M %Z}; "
            "it is not public yet, so arXiv results will be empty."
        )
    return {
        "date": d.isoformat(),
        "timezone": str(tz),
        "announced_at": announce.astimezone(tz).isoformat(),
        "arxiv_window_utc": [
            start.astimezone(dt.timezone.utc).isoformat(),
            end.astimezone(dt.timezone.utc).isoformat(),
        ],
        "hf_dates": hf_dates,
        "notes": notes,
    }


# ---------------------------------------------------------------- HTTP

_session = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def http_get(url, params=None, timeout=60, retries=4, backoff=3.0, **kw) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            r = session().get(url, params=params, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                wait = float(r.headers.get("Retry-After", backoff * (attempt + 1)))
                time.sleep(min(wait, 60))
                continue
            return r
        except requests.RequestException as e:
            last = repr(e)
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")


# ---------------------------------------------------------------- keyword matching


def normalize(text: str) -> str:
    text = text.lower().replace("‐", "-").replace("‑", "-")
    text = re.sub(r"[-_/]", " ", text)
    return re.sub(r"\s+", " ", text)


def compile_keyword(kw: str) -> re.Pattern:
    """Plain keywords are whole words, case-insensitive, with hyphens/underscores/slashes read as
    spaces. The last word also matches its plural (policy → policies, model → models) and a
    version number glued to it (ModelX → ModelX3, ModelX2.5), but no other continuation, so "VLA" does
    not match "Vlasov". Prefix with "re:" for a raw regex on the normalized (lowercased) text."""
    if kw.startswith("re:"):
        return re.compile(kw[3:], re.I)
    words = normalize(kw).split()
    if not words:
        raise ValueError(f"empty keyword {kw!r}")
    *head, last = words
    if len(last) > 2 and last.endswith("y") and last[-2] not in "aeiou":
        tail = re.escape(last[:-1]) + "(?:y|ies)"
    elif last[-1:].isalpha():
        tail = re.escape(last) + "(?:s|es)?"
    else:
        tail = re.escape(last)
    body = r"\s+".join([re.escape(w) for w in head] + [tail])
    return re.compile(r"(?<!\w)" + body + r"(?:\d[\w.]*)?(?!\w)")


def check_keyword(kw: str) -> str | None:
    """Error message for a keyword that would fail at run time, else None."""
    try:
        compile_keyword(kw[6:] if kw.startswith("title:") else kw)
        return None
    except (re.error, ValueError) as e:
        return f"{kw!r}: {e}"


def _name_tokens(name: str) -> list[str]:
    return [t.strip(".,") for t in normalize(name).split() if t.strip(".,")]


def name_matches(wanted: list[str], author: list[str]) -> bool:
    """Every token of the wanted name appears in order in the author's name, and the first and
    last tokens line up (middle names/initials are fine). Also tries family-name-first order.
    "Alex Example" matches "Example Alex" but not "Alex Other"."""
    def sub(w, a):
        if not a or a[0] != w[0] or a[-1] != w[-1]:
            return False
        it = iter(a)
        return all(any(x == y for y in it) for x in w)
    return bool(wanted) and (sub(wanted, author) or sub(wanted, author[::-1]))


def _compile_topic(topic: dict):
    if "_compiled" in topic:
        return
    comp = []
    for k in topic["keywords"]:
        title_only = k.startswith("title:")
        comp.append((k, compile_keyword(k[6:] if title_only else k), title_only))
    topic["_compiled"] = comp
    topic["_excl"] = [compile_keyword(k) for k in topic["exclude"]]
    topic["_authors"] = [(a, _name_tokens(a)) for a in topic["authors"]]


def score_topic(topic: dict, title: str, abstract: str, authors: list[str]) -> tuple[int, list[str]]:
    """Title hit = 3, abstract hit = 1 per keyword ("title:" keywords only count in the title);
    followed author/team = 5; any exclude hit -> 0."""
    _compile_topic(topic)
    t, a = normalize(title), normalize(abstract)
    if any(p.search(t) or p.search(a) for p in topic["_excl"]):
        return 0, []
    score, hits = 0, []
    for kw, p, title_only in topic["_compiled"]:
        if p.search(t):
            score += 3
            hits.append(kw)
        elif not title_only and p.search(a):
            score += 1
            hits.append(kw)
    norm_authors = [_name_tokens(x) for x in authors if x]
    for name, toks in topic["_authors"]:
        if any(name_matches(toks, x) for x in norm_authors):
            score += 5
            hits.append(f"author:{name}")
    return score, hits


# ---------------------------------------------------------------- priority authors / affiliations

_prio_cache: dict = {}


def _prio(cfg):
    key = json.dumps(cfg.get("priority") or {}, sort_keys=True, ensure_ascii=False)
    if key not in _prio_cache:
        pr = cfg.get("priority") or {}
        affs = []
        for name, aliases in (pr.get("affiliations") or {}).items():
            aliases = aliases if isinstance(aliases, list) else [aliases]
            pats = [re.compile(r"(?<!\w)" + re.escape(str(x)) + r"(?!\w)", re.I)
                    for x in aliases or [name]]
            affs.append((str(name), pats))
        people = [(str(full), _name_tokens(str(full))) for full in pr.get("authors") or []]
        _prio_cache[key] = (affs, people)
    return _prio_cache[key]


def priority_hits(cfg, authors: list[str] | None, affiliation_text: str | None) -> list[str]:
    """Names from cfg.priority that this paper matches: authors by name_matches, affiliations
    by alias on the affiliation text (author block, emails, HF organization)."""
    affs, people = _prio(cfg)
    hits = []
    norm = [_name_tokens(a) for a in (authors or []) if a]
    for full, toks in people:
        if any(name_matches(toks, t) for t in norm):
            hits.append(full)
    text = affiliation_text or ""
    for name, pats in affs:
        if any(p.search(text) for p in pats):
            hits.append(name)
    return hits


def affiliation_text(cand: dict | None, entry: dict | None) -> str:
    """Everything known about a paper's institutions: arXiv author block (or PDF title page),
    HF Daily organization, OpenAlex institutions."""
    cand = cand or {}
    return " ; ".join(filter(None, [(entry or {}).get("text"), cand.get("hf_org"),
                                    cand.get("affiliation_hint")]))


def previous_issue_date(cfg, date: str, max_back: int = 7) -> str | None:
    """The issue date (local) before `date` that had an arXiv listing."""
    tz = local_tz(cfg)
    d = dt.date.fromisoformat(date)
    for k in range(1, max_back + 1):
        x = d - dt.timedelta(days=k)
        if _announcement_on_local_date(x, tz) is not None:
            return x.isoformat()
    return None

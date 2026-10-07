#!/usr/bin/env python3
"""First-run setup and maintenance for daily-papers.

usage: setup.py status                          # JSON: what is configured / missing (cheap, run first)
       setup.py init --answers FILE [--dry-run|--force]   # preview / write the config
       setup.py schedule [--dry-run]            # install / refresh / remove the automatic run
       setup.py server [--off] [--dry-run]      # always-on local web server for output_dir
       setup.py token                           # store a GitHub token (terminal prompt, or stdin)
       setup.py github                          # create/verify the Pages repo, first push, enable Pages

Answers JSON for `init` (the agent builds it from the onboarding conversation):
{
  "topics": [{"name": "Graph Learning", "description": "...", "keywords": ["graph neural network"],
              "exclude": [], "authors": [], "min_score": 3}],
  "categories": ["cs.LG", "cs.CL", "cs.AI"],
  "priority": {"affiliations": {"Example Lab": ["Example Lab"]}, "authors": ["Alex Example"]},
  "schedule": {"enabled": true, "days": "daily", "times": ["10:30"]},
  "publish": {"enabled": true, "github_user": "octocat", "repo": "papers"},
  "feedback": {"enabled": true, "repo": "octocat/papers-feedback"},
  "notify": {"slack": {"enabled": true}},
  "output_dir": "~/daily-papers", "max_papers_per_topic": 10
}
`days` is "daily", "weekdays" or a list such as ["mon", "thu"]; times are HH:MM in KST
(schedule.timezone, default Asia/Seoul).
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from common import (SKILL_DIR, NotConfigured, check_keyword, config_path, load_config, read_json,
                    state_path, write_json)

import requests
import yaml

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_CATEGORIES = ["cs.LG", "cs.CL", "cs.CV", "cs.AI"]
GH = "https://api.github.com"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
CRON_TAG = "# daily-papers"


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def state() -> dict:
    return read_json(state_path(), {})


def save_state(**kw):
    s = state()
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k] = {**s[k], **v}
        else:
            s[k] = v
    write_json(state_path(), s)


# ---------------------------------------------------------------- schedule helpers


def norm_days(days) -> list[int]:
    if days in (None, "daily", "everyday", "매일"):
        return list(range(7))
    if days in ("weekdays", "평일"):
        return list(range(5))
    if isinstance(days, str):
        days = [d.strip() for d in days.split(",")]
    idx = sorted({DAYS.index(str(d).lower()[:3]) for d in days})
    if not idx:
        raise SystemExit("schedule.days is empty")
    return idx


def norm_times(times) -> list[str]:
    if isinstance(times, str):
        times = [times]
    res = []
    for t in times or []:
        if isinstance(t, int):  # unquoted 10:30 in YAML 1.1 is the integer 630
            t = f"{t // 60}:{t % 60}"
        h, m = (int(x) for x in str(t).strip().split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise SystemExit(f"bad time {t!r} (use HH:MM)")
        res.append(f"{h:02d}:{m:02d}")
    if not res:
        raise SystemExit("schedule.times is empty")
    return sorted(set(res))


def next_runs(sched: dict, n: int = 3) -> list[str]:
    tz = ZoneInfo(sched.get("timezone") or "Asia/Seoul")
    now = dt.datetime.now(tz)
    days, times = norm_days(sched.get("days")), norm_times(sched.get("times"))
    found = []
    for k in range(0, 15):
        d = now.date() + dt.timedelta(days=k)
        if d.weekday() not in days:
            continue
        for t in times:
            h, m = map(int, t.split(":"))
            x = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if x > now:
                found.append(x)
    return [x.strftime("%Y-%m-%d (%a) %H:%M %Z") for x in sorted(found)[:n]]


def local_slots(sched: dict) -> list[tuple[int, int, int]]:
    """(weekday Mon=0, hour, minute) in this machine's local time, for cron/launchd. Uses the
    current UTC offsets, so a DST change on either side shifts the run by an hour until rerun."""
    tz = ZoneInfo(sched.get("timezone") or "Asia/Seoul")
    local = dt.datetime.now().astimezone().tzinfo
    base = dt.datetime.now(tz).date()
    slots = set()
    for wd in norm_days(sched.get("days")):
        d = base + dt.timedelta(days=(wd - base.weekday()) % 7)
        for t in norm_times(sched.get("times")):
            h, m = map(int, t.split(":"))
            x = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz).astimezone(local)
            slots.add((x.weekday(), x.hour, x.minute))
    return sorted(slots)


def backend() -> str | None:
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)
        if r.returncode == 0:
            return "systemd"
    if shutil.which("crontab"):
        return "cron"
    return None


def run_env_path() -> str:
    dirs = [os.path.dirname(sys.executable)]
    claude = state().get("claude") or shutil.which("claude")
    if claude:
        dirs.append(os.path.dirname(claude))
    dirs += [str(Path.home() / ".local" / "bin"), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def sh(*cmd, check=True, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


# ---------------------------------------------------------------- systemd


def systemd_oncalendar(sched: dict) -> list[str]:
    days = norm_days(sched.get("days"))
    if days == list(range(7)):
        dspec = "*-*-*"
    elif days == list(range(5)):
        dspec = "Mon..Fri *-*-*"
    else:
        dspec = ",".join(DAYS[d].capitalize() for d in days) + " *-*-*"
    tz = sched.get("timezone") or "Asia/Seoul"
    return [f"OnCalendar={dspec} {t}:00 {tz}" for t in norm_times(sched.get("times"))]


def config_env_line() -> str:
    env = os.environ.get("DAILY_PAPERS_CONFIG")
    return f"\nEnvironment=DAILY_PAPERS_CONFIG={env}" if env else ""


def systemd_units(cfg) -> dict[str, str]:
    py, skill = sys.executable, SKILL_DIR
    sched = cfg["schedule"]
    return {
        "daily-papers.service": f"""[Unit]
Description=daily-papers: build the latest paper digest (and publish it)

[Service]
Type=oneshot
WorkingDirectory=%h
Environment=PATH={run_env_path()}{config_env_line()}
ExecStart={py} {skill}/scripts/autorun.py
TimeoutStartSec=72000
""",
        "daily-papers.timer": f"""[Unit]
Description=daily-papers schedule

[Timer]
{chr(10).join(systemd_oncalendar(sched))}
Persistent=true

[Install]
WantedBy=timers.target
""",
    }


def systemd_web_unit(cfg) -> str:
    return f"""[Unit]
Description=daily-papers: local web server for {cfg['output_dir']}

[Service]
Type=simple
WorkingDirectory=%h{config_env_line()}
ExecStart={sys.executable} {SKILL_DIR}/scripts/serve.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def unit_home() -> Path:
    """Real unit files live next to the config (so all of the user's files stay in one place);
    ~/.config/systemd/user only gets symlinks."""
    return config_path().resolve().parent / "systemd"


def write_unit(name: str, text: str):
    real = unit_home() / name
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(text)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    link = UNIT_DIR / name
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(real)


def remove_unit(name: str):
    (UNIT_DIR / name).unlink(missing_ok=True)
    (unit_home() / name).unlink(missing_ok=True)


def linger_ok() -> bool | None:
    r = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                       capture_output=True, text=True)
    return None if r.returncode != 0 else r.stdout.strip().endswith("yes")


# ---------------------------------------------------------------- launchd


def plist(label: str, args: list[str], extra: str) -> str:
    argx = "".join(f"<string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array>{argx}</array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{run_env_path()}</string></dict>
{extra}
</dict></plist>
"""


def launchd_load(label: str, path: Path, text: str | None):
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
    if text is None:
        path.unlink(missing_ok=True)
        return
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    sh("launchctl", "bootstrap", f"gui/{uid}", str(path))


# ---------------------------------------------------------------- commands


def importable(m: str) -> bool:
    try:
        importlib.import_module(m)
        return True
    except Exception:
        return False


def server_running(cfg) -> bool:
    s = read_json(cfg["output_dir"] / ".server.json")
    if not s:
        return False
    try:
        os.kill(s["pid"], 0)
        return True
    except OSError:
        return False


def cmd_status(_args):
    import svg_preview
    deps = {m: importable(m) for m in ("requests", "yaml", "PIL", "cryptography")}
    tools = {t: bool(shutil.which(t)) for t in ("git", "claude", "pdftotext", "pdftoppm")}
    tools["librsvg"] = svg_preview.available()
    res = {"configured": False, "config": str(config_path()), "skill_dir": str(SKILL_DIR),
           "python": sys.executable, "deps": deps, "tools": tools, "scheduler_backend": backend()}
    problems = [f"python package missing: {m}" for m, ok in deps.items() if not ok and m in ("requests", "yaml")]
    problems += [f"optional: install {m} (figure shrinking / publishing)" for m in ("PIL", "cryptography")
                 if not deps[m]]
    try:
        cfg = load_config()
    except NotConfigured:
        res["next_steps"] = ["onboarding: choose topics and optional features, then `setup.py init --answers FILE`"]
        res["problems"] = problems
        return out(res)
    res["configured"] = bool(cfg["topics"])
    sched, pc = cfg["schedule"], cfg["publish"]
    st = state()
    res["output_dir"] = str(cfg["output_dir"])
    res["topics"] = [t["name"] for t in cfg["topics"]]
    res["priority"] = cfg["priority"]
    sstate = schedule_state(cfg)
    res["schedule"] = {**sched, "state": sstate, "backend": st.get("scheduler") or backend(),
                       "next_runs": next_runs(sched) if sched.get("enabled") else []}
    key = cfg["output_dir"] / ".publish" / "key.json"
    res["publish"] = {"enabled": bool(pc.get("enabled")), "github_user": pc.get("github_user"),
                      "repo": pc.get("repo"), "url": pc.get("url"),
                      "token": Path(os.path.expanduser(pc["token_file"])).exists(),
                      "password_set": key.exists(),
                      "pages_ready": bool(st.get("github", {}).get("pages_url"))}
    fb_cfg = cfg.get("feedback") or {}
    fb_token = Path(os.path.expanduser(fb_cfg.get("token_file") or pc["token_file"]))
    res["feedback"] = {"enabled": bool(fb_cfg.get("enabled")), "repo": fb_cfg.get("repo"),
                       "server_token_set": fb_token.exists()}
    slack_cfg = (cfg.get("notify") or {}).get("slack") or {}
    slack_token = cfg["output_dir"] / ".publish" / "slack-token"
    slack_member = cfg["output_dir"] / ".publish" / "slack-member-id"
    res["notify"] = {"slack": {"enabled": bool(slack_cfg.get("enabled")),
                                "user_id_set": slack_member.exists(),
                                "token_set": slack_token.exists()}}
    res["server_running"] = server_running(cfg)
    steps = []
    if not cfg["topics"]:
        steps.append("no topics: run onboarding")
    if sstate in ("missing", "outdated"):
        steps.append(f"scheduler {sstate} (does not match config.schedule): `setup.py schedule`")
    if pc.get("enabled"):
        owner = st.get("github", {}).get("login")
        if not res["publish"]["token"]:
            steps.append("GitHub token missing: `setup.py token`")
        elif owner and pc.get("github_user") and owner.lower() != str(pc["github_user"]).lower():
            res["publish"]["token_owner"] = owner
            steps.append(f"the stored token belongs to {owner}, not {pc['github_user']}: `setup.py token`")
        if not key.exists():
            steps.append("site password missing: `publish.py set-password`")
        if not res["publish"]["pages_ready"]:
            steps.append("Pages not set up yet: `setup.py github`")
    if fb_cfg.get("enabled") and not fb_cfg.get("repo"):
        steps.append("feedback private repo missing: set `feedback.repo` in config")
    if fb_cfg.get("enabled") and not pc.get("enabled"):
        steps.append("feedback needs Pages publishing: set `publish.enabled: true`")
    if fb_cfg.get("enabled") and not fb_token.exists():
        steps.append("feedback server token missing: run `setup.py token` or set `feedback.token_file`")
    if not tools["claude"] and sched.get("enabled"):
        problems.append("`claude` CLI not found on PATH: scheduled runs need it")
    if slack_cfg.get("enabled"):
        if not sched.get("enabled"):
            steps.append("Slack DM needs a scheduled run: enable `schedule.enabled`")
        if not res["notify"]["slack"]["user_id_set"]:
            steps.append("Slack member ID missing: run `notify.py member`")
        if not res["notify"]["slack"]["token_set"]:
            steps.append("Slack bot token missing: run `notify.py token` after installing the Slack app")
    res["problems"], res["next_steps"] = problems, steps
    out(res)


def free_port(start=8000) -> int:
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def cmd_init(args):
    a = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    topics = a.get("topics") or []
    if not topics:
        raise SystemExit("answers.topics is empty: at least one topic is needed")
    names, errors = set(), []
    for t in topics:
        if not t.get("name") or not t.get("keywords"):
            raise SystemExit(f"each topic needs a name and keywords: {t}")
        if t["name"] in names:
            raise SystemExit(f"duplicate topic name {t['name']!r}")
        names.add(t["name"])
        t.setdefault("description", "")
        t.setdefault("exclude", [])
        t.setdefault("authors", [])
        errors += [f"{t['name']}: {e}" for e in map(check_keyword, t["keywords"] + t["exclude"]) if e]
    if errors:
        raise SystemExit("invalid keywords:\n  " + "\n  ".join(errors))
    path = config_path()
    old = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    sched = {"enabled": False, "days": "daily", "times": ["10:30"], "timezone": "Asia/Seoul",
             **(old.get("schedule") or {}), **(a.get("schedule") or {})}
    if sched["enabled"]:
        d = norm_days(sched["days"])
        sched["days"] = "daily" if d == list(range(7)) else "weekdays" if d == list(range(5)) \
            else [DAYS[i] for i in d]
        sched["times"] = norm_times(sched["times"])
    pub = {"enabled": False, "repo": "papers", "figure_days": 30,
           **(old.get("publish") or {}), **(a.get("publish") or {})}
    if pub["enabled"] and not pub.get("github_user"):
        raise SystemExit("publish.enabled needs publish.github_user")
    pri = (a.get("priority") if "priority" in a else old.get("priority")) or {}
    feedback = {"enabled": False, "repo": None, "min_likes": 3,
                **(old.get("feedback") or {}), **(a.get("feedback") or {})}
    if feedback["enabled"] and (not pub["enabled"] or not feedback.get("repo")):
        raise SystemExit("feedback.enabled needs publish.enabled and feedback.repo")
    old_slack = ((old.get("notify") or {}).get("slack") or {})
    answer_slack = ((a.get("notify") or {}).get("slack") or {})
    notify = {"slack": {"enabled": False, **old_slack, **answer_slack}}
    cfg = {
        "timezone": a.get("timezone") or "Asia/Seoul",
        "output_dir": a.get("output_dir") or "~/daily-papers",
        "default_lang": a.get("default_lang") or "ko",
        "max_candidates": 400,
        "max_candidates_per_topic": 60,
        "max_papers_per_topic": int(a.get("max_papers_per_topic") or 10),
        "max_also_relevant_per_topic": int(a.get("max_also_relevant_per_topic") or 30),
        "min_score": 1,
        "sources": {
            "arxiv": {"enabled": True, "categories": a.get("categories") or DEFAULT_CATEGORIES},
            "huggingface": {"enabled": True, "include_all": True},
            "openalex": {"enabled": True, "max_results_per_topic": 40},
        },
        "fetch": {"max_appendix_figures": 8, "max_content_chars": 140000, "workers": 3},
        "serve": {"host": "127.0.0.1", "port": free_port(8000)},
        "schedule": sched,
        "publish": pub,
        "feedback": feedback,
        "notify": notify,
        "priority": {"affiliations": pri.get("affiliations") or {}, "authors": pri.get("authors") or []},
        "topics": topics,
    }
    # Reconfiguring keeps whatever the answers don't cover (server port, fetch limits, publish
    # paths, hand edits); only topics/categories/priority/schedule/publish basics are replaced.
    for k in ("max_candidates", "max_candidates_per_topic", "max_also_relevant_per_topic", "min_score", "fetch",
              "serve", "default_lang"):
        if k in old and k not in a:
            cfg[k] = old[k]
    if "sources" in old:
        cfg["sources"] = {**old["sources"], "arxiv": {**old["sources"].get("arxiv", {}),
                                                      "categories": cfg["sources"]["arxiv"]["categories"]}}
    if isinstance(old.get("publish"), dict):
        cfg["publish"] = {**old["publish"], **pub}
    if args.dry_run:
        return out({"dry_run": True, "config_path": str(path), "would_replace": path.exists(),
                    "next_runs": next_runs(sched) if sched["enabled"] else [],
                    "config": cfg})
    if path.exists():
        if not args.force:
            raise SystemExit(f"{path} already exists; pass --force to replace it (a backup is kept)")
        shutil.copy2(path, path.with_name(f"config.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# daily-papers settings, written by onboarding (setup.py init).\n"
              f"# Every field is explained in {SKILL_DIR / 'config.example.yaml'}.\n"
              "# Edit freely, or ask the agent (e.g. \"add a topic\", \"change the schedule\").\n\n")
    path.write_text(header + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=100),
                    encoding="utf-8")
    load_config()  # validate round trip
    steps = []
    if sched["enabled"]:
        steps.append("setup.py schedule")
    steps.append("setup.py server  (optional: always-on local web server)")
    if pub["enabled"]:
        steps += ["setup.py token", "publish.py set-password", "setup.py github"]
    if feedback["enabled"]:
        steps.append("create the private feedback repo and a browser token for that repo")
    if notify["slack"]["enabled"]:
        steps += ["notify.py token", "notify.py member", "notify.py test"]
    out({"config": str(path), "topics": [t["name"] for t in topics],
         "next_runs": next_runs(sched) if sched["enabled"] else [], "next_steps": steps})


def launchd_autorun_plist(cfg) -> str:
    weekday = lambda d: (d + 1) % 7  # noqa: E731  launchd: 0 = Sunday
    entries = "".join(
        f"<dict><key>Weekday</key><integer>{weekday(d)}</integer><key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>{m}</integer></dict>" for d, h, m in local_slots(cfg["schedule"]))
    return plist("com.dailypapers.autorun", [sys.executable, f"{SKILL_DIR}/scripts/autorun.py"],
                 f"<key>StartCalendarInterval</key><array>{entries}</array>\n"
                 f"<key>StandardOutPath</key><string>{cfg['output_dir']}/.autorun-launchd.log</string>\n"
                 f"<key>StandardErrorPath</key><string>{cfg['output_dir']}/.autorun-launchd.log</string>")


def cron_lines(cfg) -> list[str]:
    by_time: dict[tuple[int, int], list[int]] = {}
    for d, h, m in local_slots(cfg["schedule"]):
        by_time.setdefault((h, m), []).append(d)
    env = os.environ.get("DAILY_PAPERS_CONFIG")
    prefix = f"DAILY_PAPERS_CONFIG={env} " if env else ""
    return [f"{m} {h} * * {'*' if len(ds) == 7 else ','.join(str((d + 1) % 7) for d in ds)} "
            f"{prefix}{sys.executable} {SKILL_DIR}/scripts/autorun.py "
            f">> {cfg['output_dir']}/.autorun-cron.log 2>&1 {CRON_TAG}"
            for (h, m), ds in sorted(by_time.items())]


def schedule_state(cfg) -> str:
    """ok | outdated (installed but differs from the config) | missing | off."""
    sched, be = cfg["schedule"], state().get("scheduler") or backend()
    try:
        if be == "systemd":
            want = systemd_units(cfg)
            have = {n: (UNIT_DIR / n).read_text() if (UNIT_DIR / n).exists() else None for n in want}
        elif be == "launchd":
            p = AGENT_DIR / "com.dailypapers.autorun.plist"
            want, have = {"p": launchd_autorun_plist(cfg)}, {"p": p.read_text() if p.exists() else None}
        elif be == "cron":
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
            got = [ln for ln in cur.splitlines() if CRON_TAG in ln]
            want, have = {"c": "\n".join(cron_lines(cfg))}, {"c": "\n".join(got) if got else None}
        else:
            return "missing" if sched.get("enabled") else "off"
    except SystemExit:  # invalid schedule values
        return "outdated"
    present = any(v is not None for v in have.values())
    if not sched.get("enabled"):
        return "outdated" if present else "off"
    if not present:
        return "missing"
    return "ok" if have == want else "outdated"


def cmd_schedule(args):
    cfg = load_config()
    sched = cfg["schedule"]
    be = backend()
    st = state()
    claude = shutil.which("claude") or st.get("claude")
    if sched.get("enabled") and not claude:
        raise SystemExit("`claude` CLI not found on PATH; scheduled runs need it")
    if not args.dry_run:
        save_state(claude=claude, scheduler=be,
                   schedule_since=st.get("schedule_since") or dt.date.today().isoformat())
    res = {"backend": be, "enabled": bool(sched.get("enabled"))}
    if be == "systemd":
        units = systemd_units(cfg)
        if args.dry_run:
            return out({**res, "units": units})
        if sched.get("enabled"):
            for name, text in units.items():
                write_unit(name, text)
            sh("systemctl", "--user", "daemon-reload")
            sh("systemctl", "--user", "enable", "--now", "daily-papers.timer")
            sh("systemctl", "--user", "restart", "daily-papers.timer")
            res["linger"] = linger_ok()
            if res["linger"] is False:
                res["warning"] = ("lingering is off: runs only happen while you are logged in. "
                                  f"Enable it with `loginctl enable-linger {os.environ.get('USER', '')}`")
        else:
            sh("systemctl", "--user", "disable", "--now", "daily-papers.timer", check=False)
            for name in units:
                remove_unit(name)
            sh("systemctl", "--user", "daemon-reload")
    elif be == "launchd":
        text = launchd_autorun_plist(cfg)
        if args.dry_run:
            return out({**res, "plist": text})
        launchd_load("com.dailypapers.autorun", AGENT_DIR / "com.dailypapers.autorun.plist",
                     text if sched.get("enabled") else None)
        res["note"] = "launchd uses local time; rerun `setup.py schedule` after a DST change"
    elif be == "cron":
        lines = cron_lines(cfg)
        if args.dry_run:
            return out({**res, "cron": lines})
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if cur.returncode != 0 and "no crontab" not in (cur.stderr or "").lower():
            raise SystemExit(f"crontab is not usable here: {cur.stderr.strip()[:300]}")
        keep = [ln for ln in cur.stdout.splitlines() if CRON_TAG not in ln]
        new = keep + (lines if sched.get("enabled") else [])
        sh("crontab", "-", input="\n".join(new) + "\n")
        res["note"] = "cron uses local time; rerun `setup.py schedule` after a DST change"
    else:
        raise SystemExit("no scheduler found (systemd --user, launchd or crontab); run autorun.py manually")
    if sched.get("enabled"):
        res["next_runs"] = next_runs(sched)
    out(res)


def cmd_server(args):
    cfg = load_config()
    be = backend()
    if be == "systemd":
        text = systemd_web_unit(cfg)
        if args.dry_run:
            return out({"backend": be, "unit": text})
        if args.off:
            sh("systemctl", "--user", "disable", "--now", "daily-papers-web.service", check=False)
            remove_unit("daily-papers-web.service")
            sh("systemctl", "--user", "daemon-reload")
            return out({"server": "removed"})
        if server_running(cfg):  # a manually started server holds the port; replace it
            subprocess.run([sys.executable, f"{SKILL_DIR}/scripts/serve.py", "--stop"], capture_output=True)
        write_unit("daily-papers-web.service", text)
        sh("systemctl", "--user", "daemon-reload")
        sh("systemctl", "--user", "enable", "--now", "daily-papers-web.service")
        sh("systemctl", "--user", "restart", "daily-papers-web.service")
    elif be == "launchd":
        text = plist("com.dailypapers.web", [sys.executable, f"{SKILL_DIR}/scripts/serve.py"],
                     "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>")
        if args.dry_run:
            return out({"backend": be, "plist": text})
        if not args.off and server_running(cfg):
            subprocess.run([sys.executable, f"{SKILL_DIR}/scripts/serve.py", "--stop"], capture_output=True)
        launchd_load("com.dailypapers.web", AGENT_DIR / "com.dailypapers.web.plist", None if args.off else text)
        if args.off:
            return out({"server": "removed"})
    else:
        return out({"server": "no service manager here",
                    "hint": f"start it manually: nohup {sys.executable} {SKILL_DIR}/scripts/serve.py &"})
    time.sleep(1.5)
    out({"server": "running" if server_running(cfg) else "starting",
         "url": f"http://localhost:{cfg['serve']['port']}/"})


def gh_headers(token: str) -> dict:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def cmd_token(_args):
    cfg = load_config()
    pc = cfg["publish"]
    if sys.stdin.isatty():
        tok = getpass.getpass("GitHub token (입력은 화면에 보이지 않습니다): ").strip()
    else:
        tok = sys.stdin.readline().strip()
    if not tok:
        raise SystemExit("empty token")
    r = requests.get(f"{GH}/user", headers=gh_headers(tok), timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"GitHub rejected the token (HTTP {r.status_code})")
    me = r.json()
    if pc.get("github_user") and me["login"].lower() != str(pc["github_user"]).lower():
        raise SystemExit(f"token belongs to {me['login']}, but publish.github_user is {pc['github_user']}")
    tf = Path(os.path.expanduser(pc["token_file"]))
    tf.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(tf.parent, 0o700)
    fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    save_state(github={"login": me["login"], "id": me["id"],
                       "email": f"{me['id']}+{me['login']}@users.noreply.github.com"})
    scopes = r.headers.get("X-OAuth-Scopes")
    out({"login": me["login"], "token_file": str(tf), "scopes": scopes or "(fine-grained token)",
         "note": "Automatic Pages setup needs a fine-grained token with Contents, Pages and "
                 "Administration write on the selected repo, or a classic token with repo scope. "
                 "public_repo alone may need manual Pages setup."})


def cmd_github(_args):
    cfg = load_config()
    pc = cfg["publish"]
    if not pc.get("enabled") or not pc.get("github_user"):
        raise SystemExit("publish is not enabled in config (publish.enabled / publish.github_user)")
    tf = Path(os.path.expanduser(pc["token_file"]))
    if not tf.exists():
        raise SystemExit("no GitHub token yet: run `setup.py token` first")
    if not (cfg["output_dir"] / ".publish" / "key.json").exists():
        raise SystemExit("no site password yet: run `publish.py set-password` first")
    H = gh_headers(tf.read_text().strip())
    owner, repo, branch = pc["github_user"], pc.get("repo") or "papers", pc.get("branch") or "main"
    me = requests.get(f"{GH}/user", headers=H, timeout=30).json()
    save_state(github={"login": me["login"], "id": me["id"],
                       "email": f"{me['id']}+{me['login']}@users.noreply.github.com"})
    res = {"repo": f"{owner}/{repo}"}
    r = requests.get(f"{GH}/repos/{owner}/{repo}", headers=H, timeout=30)
    if r.status_code == 404:
        c = requests.post(f"{GH}/user/repos", headers=H, timeout=30, json={
            "name": repo, "private": False, "has_issues": False, "has_wiki": False,
            "has_projects": False, "description": "Encrypted daily paper digest (daily-papers skill)"})
        if c.status_code not in (200, 201):
            raise SystemExit(f"could not create {owner}/{repo} (HTTP {c.status_code}). Create an empty "
                             f"public repo named '{repo}' on github.com yourself, then rerun this command.")
        res["created"] = True
    elif r.status_code == 200 and r.json().get("private"):
        res["warning"] = ("the repo is private: GitHub Pages on the Free plan needs a public repo "
                          "(the published content is encrypted either way)")
    elif r.status_code != 200:
        raise SystemExit(f"cannot read {owner}/{repo} (HTTP {r.status_code})")

    subprocess.run([sys.executable, str(SKILL_DIR / "scripts" / "render.py"), "--archive-only"],
                   capture_output=True)  # archive page + assets exist even before the first issue
    import publish
    cfg = load_config()  # picks up the account email saved above
    res["push"] = publish.push(cfg, publish.key_path(cfg))

    p = requests.get(f"{GH}/repos/{owner}/{repo}/pages", headers=H, timeout=30)
    if p.status_code == 404:
        e = requests.post(f"{GH}/repos/{owner}/{repo}/pages", headers=H, timeout=30,
                          json={"source": {"branch": branch, "path": "/"}, "build_type": "legacy"})
        if e.status_code not in (200, 201):
            res["pages"] = (f"could not enable Pages via API (HTTP {e.status_code}). On github.com open "
                            f"{owner}/{repo} → Settings → Pages → Deploy from a branch → {branch} / (root), "
                            "then rerun this command.")
            return out(res)
    url = None
    for _ in range(36):
        info = requests.get(f"{GH}/repos/{owner}/{repo}/pages", headers=H, timeout=30).json()
        url = info.get("html_url") or url
        if info.get("status") == "built" and url:
            if requests.get(url, timeout=30).status_code == 200:
                break
        time.sleep(5)
    if url:
        save_state(github={"pages_url": url})
    res["pages_url"] = url
    res["live"] = bool(url) and requests.get(url, timeout=30).status_code == 200
    out(res)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("init")
    p.add_argument("--answers", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="validate and preview, write nothing")
    p = sub.add_parser("schedule")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("server")
    p.add_argument("--off", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("token")
    sub.add_parser("github")
    args = ap.parse_args()
    {"status": cmd_status, "init": cmd_init, "schedule": cmd_schedule, "server": cmd_server,
     "token": cmd_token, "github": cmd_github}[args.cmd](args)


if __name__ == "__main__":
    main()

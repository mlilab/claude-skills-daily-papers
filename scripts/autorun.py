#!/usr/bin/env python3
"""Scheduled run (started by the timer/agent/cron that `setup.py schedule` installs): build the
digest only when needed.

usage: autorun.py [--date D] [--dry-run] [--force]

For the latest published arXiv issue (or --date) it decides:
  skip-up-to-date   render.py's .rendered.json says every selected paper is summarized and valid
  build             run `claude -p` headless with the daily-papers skill
It also catches up issues missed since the newest complete one (machine off, failed run, arXiv
down, or a schedule less frequent than daily), up to MAX_CATCHUP, never before the schedule
was installed.
Afterwards it pushes the encrypted copy to GitHub Pages (publish.py push; a no-op if unchanged)
and sends one Slack DM for the target issue, including failures and up-to-date skips.
Log: <output_dir>/.autorun.log (one JSON line per decision); Claude's output goes to
<output_dir>/.autorun-claude.log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

from common import (SKILL_DIR, issue_dir, load_config, local_tz, previous_issue_date, read_json,
                    resolve_issue, state_path)

PROMPT = ("[daily-papers:auto] daily-papers 스킬로 {date} issue 논문 다이제스트를 만들어줘. "
          "자동 실행이므로 사용자에게 질문하지 말고 끝까지 완료해. 모든 스크립트에 --date {date}를 "
          "지정하고 {skill}/scripts/ 의 절대 경로로 실행해. config.yaml은 수정하지 마. 웹 서버는 "
          "systemd(daily-papers-web.service)가 관리하므로 따로 띄우지 않아도 돼.")
ALLOWED = ["Bash(python3:*)", "Bash(cd:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)",
           "Bash(sed:*)", "Bash(wc:*)", "Bash(grep:*)", "Bash(echo:*)", "Bash(jq:*)",
           "Bash(tail:*)", "Bash(sleep:*)", "Read", "Write", "Edit", "Glob", "Grep",
           "Agent"]
TIMEOUT_MIN = 150
MAX_CATCHUP = 6  # older issues built in one run, besides the latest


def logline(cfg, rec: dict):
    rec = {"at": dt.datetime.now(local_tz(cfg)).isoformat(timespec="seconds"), **rec}
    p = cfg["output_dir"] / ".autorun.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)


def issue_status(cfg, date: str) -> dict:
    r = read_json(issue_dir(cfg, date) / ".rendered.json")
    issue = read_json(issue_dir(cfg, date) / "issue.json", {})
    complete = (bool(r) and r.get("problems", 1) == 0 and r.get("summarized") == r.get("papers")
                and not r.get("source_errors") and not issue.get("manual_only"))
    return {"date": date, "built": bool(r), "complete": complete,
            "action": "skip-up-to-date" if complete else "build"}


def run_claude(cfg, date: str) -> dict:
    claude = (read_json(state_path(), {}).get("claude") or shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
    cmd = [claude, "-p", PROMPT.format(date=date, skill=SKILL_DIR),
           "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED]
    out = cfg["output_dir"] / ".autorun-claude.log"
    t0 = time.time()
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"\n===== {dt.datetime.now(local_tz(cfg)).isoformat(timespec='seconds')} {date}\n")
        f.flush()
        try:
            # run from $HOME so these sessions never count as workspace activity elsewhere
            res = subprocess.run(cmd, cwd=os.path.expanduser("~"), stdout=f, stderr=subprocess.STDOUT,
                                 timeout=TIMEOUT_MIN * 60)
            code = res.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
        except OSError as e:
            code = f"error: {e}"
    st = issue_status(cfg, date)
    r = read_json(issue_dir(cfg, date) / ".rendered.json") or {}
    return {"exit": code, "minutes": round((time.time() - t0) / 60, 1),
            "complete": st["complete"], "papers": r.get("papers"), "others": r.get("others")}


def refresh_votes(cfg):
    """Pull the latest 👍/👎 and re-render all issues, so liked papers move to the top and
    disliked ones to the bottom of their topic on every page (render is cheap and keeps each
    issue's frozen priority list)."""
    if not (cfg.get("feedback") or {}).get("enabled"):
        return True
    try:
        import feedback
        res = feedback.sync(cfg)
        r = subprocess.run([sys.executable, str(SKILL_DIR / "scripts" / "render.py"), "--all"],
                           capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0 and not res.get("error")
        logline(cfg, {"action": "votes", **res, "rerender_ok": r.returncode == 0})
        return ok
    except Exception as e:
        logline(cfg, {"action": "votes", "error": str(e)[:300]})
        return False


def notify_slack(cfg, date: str, *, ok: bool, reasons: list[str], published: bool | None):
    """Report the scheduled run outcome, even when build or publishing failed."""
    try:
        import notify
        if not notify.enabled(cfg):
            logline(cfg, {"action": "slack", "date": date, "skipped": "not configured"})
            return None
        notify.dm(cfg, notify.report_text(cfg, date, ok=ok, reasons=reasons, published=published))
        logline(cfg, {"action": "slack", "date": date, "ok": True})
        return True
    except Exception as e:
        logline(cfg, {"action": "slack", "date": date, "ok": False, "error": str(e)[:300]})
        return False


def publish(cfg):
    """Push the encrypted copy to GitHub Pages. Cheap when nothing changed (no-op push), so it runs
    after every autorun; it also drops figures that aged past publish.figure_days."""
    if not (cfg.get("publish") or {}).get("enabled"):
        return None
    try:
        res = subprocess.run([sys.executable, str(SKILL_DIR / "scripts" / "publish.py"), "push"],
                             capture_output=True, text=True, timeout=1800)
        out = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None
        logline(cfg, {"action": "publish", "ok": res.returncode == 0,
                      **({k: out.get(k) for k in ("pushed", "reason", "pages", "images", "megabytes")}
                         if out else {"error": (res.stderr or res.stdout)[-400:]})})
        return res.returncode == 0
    except Exception as e:  # never let publishing break the digest run
        logline(cfg, {"action": "publish", "ok": False, "error": str(e)[:400]})
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="issue date to build (default: latest published)")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, don't start Claude")
    ap.add_argument("--force", action="store_true", help="build even if complete")
    args = ap.parse_args()
    cfg = load_config()

    lock = cfg["output_dir"] / ".state" / "autorun.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            pid = int(lock.read_text().strip() or 0)
            os.kill(pid, 0)
            logline(cfg, {"action": "skip-already-running", "pid": pid})
            return
        except (OSError, ValueError):
            lock.unlink(missing_ok=True)
            return main()
    target = args.date or dt.datetime.now(local_tz(cfg)).date().isoformat()
    reasons = []
    published = False if (cfg.get("publish") or {}).get("enabled") else None
    try:
        target = args.date or resolve_issue(cfg, None)["date"]
        todo = []
        if not args.date:
            # Catch up issues missed since the newest complete one (machine off, a failed run, or
            # a schedule that runs less often than arXiv publishes, e.g. weekly), oldest first.
            # Never reach back before the schedule was installed or the first ever build.
            complete = [p.name for p in cfg["output_dir"].glob("????-??-??")
                        if issue_status(cfg, p.name)["complete"]]
            floor = max(filter(None, [max(complete, default=None),
                                      read_json(state_path(), {}).get("schedule_since")]), default=None)
            d = target
            while floor and len(todo) < MAX_CATCHUP:
                d = previous_issue_date(cfg, d)
                if not d or d <= floor:
                    break
                if not issue_status(cfg, d)["complete"]:
                    todo.insert(0, issue_status(cfg, d))
        st = issue_status(cfg, target)
        if args.force:
            st["action"], st["forced"] = "build", True
        todo.append(st)
        for s in todo:
            if s["action"] != "build" or args.dry_run:
                logline(cfg, {**s, "dry_run": args.dry_run})
                continue
            logline(cfg, {**s, "started": True})
            res = run_claude(cfg, s["date"])
            logline(cfg, {**s, "result": res})
            if res.get("exit") != 0 or not res.get("complete"):
                reasons.append(f"{s['date']} 빌드 실패")
        if not args.dry_run:
            if not refresh_votes(cfg):
                reasons.append("투표 반영 실패")
            published = publish(cfg)
            if published is False:
                reasons.append("GitHub Pages 게시 실패")
            if not issue_status(cfg, target)["complete"]:
                published = False
                if not any("빌드 실패" in x for x in reasons):
                    reasons.append(f"{target} 빌드 미완료")
    except BaseException as e:
        logline(cfg, {"action": "autorun-error", "date": target, "error": str(e)[:300]})
        reasons.append("자동 실행 중 예외 발생")
    finally:
        if not args.dry_run:
            if notify_slack(cfg, target, ok=not reasons, reasons=reasons, published=published) is False:
                reasons.append("Slack 알림 실패")
        lock.unlink(missing_ok=True)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())

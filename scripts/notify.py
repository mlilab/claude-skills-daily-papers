#!/usr/bin/env python3
"""Send a scheduled-run result to the configured Slack member by bot DM.

usage: notify.py token            # securely store and validate the xoxb token
       notify.py member           # securely store and validate one recipient member ID
       notify.py test             # send a test DM
       notify.py send --date D    # resend a successful issue report
       notify.py preview --date D # print a report without sending it
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

from common import (issue_dir, load_config, load_requested, paper_dirname, read_json, session,
                    with_requested_candidates, with_requested_selection)


def token_path(cfg) -> Path:
    return cfg["output_dir"] / ".publish" / "slack-token"


def member_path(cfg) -> Path:
    return cfg["output_dir"] / ".publish" / "slack-member-id"


def member_id(cfg) -> str:
    user = member_path(cfg).read_text().strip()
    if not re.fullmatch(r"[UW][A-Z0-9]{8,}", user):
        raise ValueError("stored Slack member ID is invalid")
    return user


def slack(cfg, method: str, payload: dict) -> dict:
    tok = token_path(cfg).read_text().strip()
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {tok}"}
    if method == "users.info":
        r = session().get(url, params=payload, timeout=30, headers=headers)
    else:
        r = session().post(url, json=payload, timeout=30,
                           headers={**headers, "Content-Type": "application/json; charset=utf-8"})
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"Slack {method}: {d.get('error')}")
    return d


def dm(cfg, text: str):
    import publish
    for name, pat in publish.leak_patterns(cfg):
        if pat.search(text):
            raise ValueError(f"Slack DM refused: {name} found in the message")
    user = member_id(cfg)
    ch = slack(cfg, "conversations.open", {"users": user})["channel"]["id"]
    slack(cfg, "chat.postMessage", {"channel": ch, "text": text, "unfurl_links": False,
                                    "unfurl_media": False})


def issue_counts(cfg, date: str) -> list[tuple[str, int | None, int | None, int | None]]:
    """Candidate matches can overlap topics; detailed counts include summaries already written."""
    root = issue_dir(cfg, date)
    def safe_json(path, default):
        try:
            return read_json(path, default)
        except (OSError, ValueError, UnicodeError):
            return default

    requested = load_requested(cfg, date)
    raw_candidates = safe_json(root / "candidates.json", None)
    raw_selected = safe_json(root / "selected.json", None)
    candidates_ready = isinstance(raw_candidates, list) or bool(requested.get("candidates"))
    selected_ready = (isinstance(raw_selected, dict) and isinstance(raw_selected.get("papers"), list)
                      or bool(requested.get("papers")))
    candidates = with_requested_candidates(raw_candidates or [], requested)
    selected = with_requested_selection(raw_selected, requested)
    candidate_counts = {}
    manual_ids = set((requested.get("candidates") or {}).keys())
    for candidate in candidates if candidates_ready else []:
        if isinstance(candidate, dict):
            seen = set()
            for match in candidate.get("topic_matches") or []:
                name = match.get("topic") if isinstance(match, dict) else None
                if not isinstance(name, str) or name in seen:
                    continue
                seen.add(name)
                candidate_counts[name] = candidate_counts.get(name, 0) + 1
            if not seen and candidate.get("id") in manual_ids:
                candidate_counts["Others"] = candidate_counts.get("Others", 0) + 1
    detailed, others, slots = {}, {}, {}
    cap = cfg.get("max_papers_per_topic")
    for entry in selected["papers"] if selected_ready else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        topics = entry.get("topics")
        name = topics[0] if isinstance(topics, list) and topics and isinstance(topics[0], str) else "Other"
        if entry.get("summarize", True) and (entry.get("requested") or not cap or slots.get(name, 0) < int(cap)):
            if not entry.get("requested"):
                slots[name] = slots.get(name, 0) + 1
            summary = safe_json(root / "papers" / paper_dirname(entry["id"]) / "summary.json", None)
            if isinstance(summary, dict) and summary.get("en") and summary.get("ko"):
                detailed[name] = detailed.get(name, 0) + 1
        else:
            others[name] = others.get(name, 0) + 1
    other_cap = cfg.get("max_also_relevant_per_topic", cfg.get("max_also_relevant"))
    if other_cap is not None:
        others = {name: min(n, int(other_cap)) for name, n in others.items()}
    names = [t["name"] for t in cfg["topics"]]
    names += [n for n in sorted(candidate_counts.keys() | detailed.keys() | others.keys())
              if n not in names]
    return [(n, candidate_counts.get(n, 0) if candidates_ready else None,
             detailed.get(n, 0) if selected_ready else None,
             others.get(n, 0) if selected_ready else None)
            for n in names]


def report_text(cfg, date: str, *, ok: bool, reasons: list[str] | None = None,
                published: bool | None = None) -> str:
    """Report what exists at termination, including partial files after a failed build."""
    counts = issue_counts(cfg, date)
    lines = [f"📚 Daily Papers {date}",
             f"종료 상태: {'정상 종료' if ok else '에러 종료'}"]
    if reasons:
        lines.append("상세: " + ", ".join(reasons))
    lines += ["", "주제 | 후보 | 상세 요약 | 추가 관련"]
    lines += [f"• {n}: {c if c is not None else '—'} / {p if p is not None else '—'} / "
              f"{o if o is not None else '—'}" for n, c, p, o in counts]
    lines.append("후보는 키워드 매칭 수이며 주제 사이에 중복될 수 있습니다. 요약과 추가 관련은 사이트 표시 기준입니다.")
    if any(value is None for row in counts for value in row[1:]):
        lines.append("—: 해당 단계의 파일이 없어 집계할 수 없습니다.")
    url = (cfg.get("publish") or {}).get("url")
    if url:
        lines += ["", f"게시 URL: {url.rstrip('/')}/{date}/"]
        if published is False:
            lines.append("이 날짜의 완성본은 이번 실행에서 게시되지 않았습니다. URL은 이전 내용이거나 접근할 수 없을 수 있습니다.")
    return "\n".join(lines)


def enabled(cfg) -> bool:
    s = (cfg.get("notify") or {}).get("slack") or {}
    return bool(s.get("enabled") and member_path(cfg).exists() and token_path(cfg).exists())


def cmd_token(cfg):
    tok = getpass.getpass("Slack bot token (xoxb-…, 화면에 보이지 않음): ").strip() if sys.stdin.isatty() \
        else sys.stdin.readline().strip()
    if not tok.startswith("xoxb-"):
        raise SystemExit("not a bot token (expected xoxb-…)")
    r = session().post("https://slack.com/api/auth.test", json={}, timeout=30,
                       headers={"Authorization": f"Bearer {tok}"})
    info = r.json()
    if not info.get("ok"):
        raise SystemExit(f"Slack auth.test failed: {info.get('error')}")
    p = token_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.chmod(p, 0o600)
    print(json.dumps({"stored": True, "team": info.get("team"), "bot": info.get("user")}, ensure_ascii=False))


def cmd_member(cfg):
    if not token_path(cfg).exists():
        raise SystemExit("store the Slack bot token first: run `notify.py token`")
    user = getpass.getpass("Recipient Slack member ID (hidden): ").strip() if sys.stdin.isatty() \
        else sys.stdin.readline().strip()
    if not re.fullmatch(r"[UW][A-Z0-9]{8,}", user):
        raise SystemExit("invalid Slack member ID")
    info = slack(cfg, "users.info", {"user": user}).get("user") or {}
    if info.get("id") != user or info.get("is_bot") or info.get("deleted"):
        raise SystemExit("Slack member is not an active human account")
    p = member_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(user)
    os.chmod(p, 0o600)
    print(json.dumps({"recipient_stored": True, "active_human": True}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["token", "member", "test", "send", "preview"])
    ap.add_argument("--date")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "token":
        return cmd_token(cfg)
    if args.cmd == "member":
        return cmd_member(cfg)
    if args.cmd == "test":
        if not token_path(cfg).exists():
            raise SystemExit("no Slack bot token yet: run `notify.py token`")
        if not member_path(cfg).exists():
            raise SystemExit("no Slack recipient yet: run `notify.py member`")
        dm(cfg, "✅ Daily Papers 알림 연결 테스트입니다. 매일 실행이 종료되면 결과를 여기로 알려 드립니다.")
        print(json.dumps({"sent": True}))
        return
    if not args.date:
        raise SystemExit(f"{args.cmd} needs --date")
    if args.cmd == "send" and not read_json(issue_dir(cfg, args.date) / ".rendered.json"):
        raise SystemExit(f"{args.date} is not rendered yet")
    msg = report_text(cfg, args.date, ok=True, published=True)
    if args.cmd == "preview":
        print(msg)
        return
    if not token_path(cfg).exists():
        raise SystemExit("no Slack bot token yet: run `notify.py token`")
    if not member_path(cfg).exists():
        raise SystemExit("no Slack recipient yet: run `notify.py member`")
    dm(cfg, msg)
    print(json.dumps({"sent": True, "date": args.date}))


if __name__ == "__main__":
    main()

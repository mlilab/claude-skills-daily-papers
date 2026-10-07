#!/usr/bin/env python3
"""Lint the English fields of summary.json files against ASD-STE100 structural rules.

usage: ste_check.py <paper_dir> [<paper_dir> ...]

Uses the vendored linter (vendor/asd-ste100/scripts/ste-lint.py). LaTeX math and Markdown
markup are masked first so formulas do not count as words. Prints, per paper, the hard
violations (semicolons, sentences over 25 words, phrasal verbs, nominalizations, marketing
adjectives, synonym rotation, dangling conjunctions) with the field they occur in, plus a
count of advisory findings (passive voice, compound tenses). Always exits 0: fix hard
violations unless the fix would drop precision.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

LINT = Path(__file__).resolve().parent.parent / "vendor" / "asd-ste100" / "scripts" / "ste-lint.py"
FIELDS = ["tldr", "key_points", "problem", "method", "results", "why_it_matters", "limitations",
          "relevance"]


def load_linter():
    spec = importlib.util.spec_from_file_location("ste_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean(text: str) -> str:
    text = re.sub(r"\$\$.+?\$\$|\$[^$\n]+?\$", "X", text, flags=re.S)  # a formula reads as one word
    text = re.sub(r"\*\*|`|\*", "", text)
    return text


def skill_receipt() -> str:
    """'<name> <version>' from the vendored STE skill's frontmatter. A summary must carry it in its
    "ste" field, which proves the writer opened vendor/asd-ste100/SKILL.md."""
    head = (LINT.parent.parent / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    name = re.search(r"^name:\s*(\S+)", head, re.M).group(1)
    version = re.search(r"^version:\s*(\S+)", head, re.M).group(1)
    return f"{name} {version}"


def check(paper_dir: Path, lint_mod) -> dict:
    s = json.loads((paper_dir / "summary.json").read_text(encoding="utf-8"))
    en = s.get("en") or {}
    parts = []
    for k in FIELDS:
        v = en.get(k)
        if isinstance(v, list):
            parts += [(f"{k}[{i}]", x) for i, x in enumerate(v)]
        elif v:
            parts.append((k, v))
    parts += [(f"figure {f.get('asset')}", f.get("en", "")) for f in s.get("figures", []) if f.get("en")]
    hard, advisory = [], 0
    for field, text in parts:
        findings, _ = lint_mod.lint(clean(str(text)), filename=field)
        for f in findings:
            if f["level"] == "advisory-free":
                hard.append({"field": field, "rule": f["rule"], "match": f["match"], "fix": f["message"]})
            else:
                advisory += 1
    if s.get("ste") != skill_receipt():
        hard.append({"field": "ste", "rule": "ste-skill-not-read",
                     "match": str(s.get("ste")),
                     "fix": "Open and read vendor/asd-ste100/SKILL.md in full, then set summary.json "
                            "\"ste\" to the skill name and version from its frontmatter, as \"<name> <version>\"."})
    return {"paper": paper_dir.name, "hard_violations": len(hard), "advisory": advisory, "details": hard}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    lint_mod = load_linter()
    out = []
    for d in sys.argv[1:]:
        p = Path(d)
        try:
            out.append(check(p, lint_mod))
        except FileNotFoundError:
            out.append({"paper": p.name, "error": "summary.json not found"})
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Regression checks for reader-requested papers; no network or published files are changed."""
import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import autorun
import feedback
import fetch_papers
import notify
import render
import request_paper
import ste_check
from common import load_requested, read_json, with_requested_candidates, with_requested_selection, write_json


class RequestedPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {
            "output_dir": self.root, "timezone": "Asia/Seoul", "default_lang": "ko",
            "fetch": {"workers": 1, "max_content_chars": 10000},
            "topics": [{"name": "Graph Learning", "description": "Graph representation methods.",
                        "keywords": ["graph transformer", "message passing"], "exclude": [], "authors": [],
                        "categories": []}],
            "priority": {"affiliations": {}, "authors": []},
            "max_papers_per_topic": 2, "max_also_relevant_per_topic": 30,
            "feedback": {"enabled": True, "repo": "example/private", "min_likes": 3,
                         "token_file": str(self.root / "token")},
            "publish": {"enabled": True, "url": "https://example.github.io/papers/",
                        "token_file": str(self.root / "token")},
        }
        self.date = "2025-01-12"  # Sunday: the scheduled arXiv issue resolver rejects this date.

    def prepare(self, pid, title, abstract):
        path = self.root / "record.json"
        path.write_text(json.dumps({"id": pid, "title": title, "abstract": abstract,
                                    "authors": ["A. Example"],
                                    "links": {"abs": "https://example.org/paper"}}))
        return request_paper.prepare(self.cfg, self.date, None, str(path))

    def test_prepare_classifies_and_survives_daily_rewrite(self):
        first = self.prepare("test:graph", "Graph Transformer for Networks",
                             "We use message passing to train a graph model.")
        second = self.prepare("test:other", "Minerals in volcanic rocks",
                              "We measure basalt samples with spectroscopy.")
        self.assertEqual(first["topics"], ["Graph Learning"])
        self.assertEqual(second["topics"], ["Others"])
        self.assertTrue(read_json(self.root / self.date / "issue.json")["manual_only"])
        saved = load_requested(self.cfg, self.date)
        self.assertEqual(len(with_requested_selection({"papers": []}, saved)["papers"]), 2)
        self.assertEqual(len(with_requested_candidates([], saved)), 2)
        write_json(self.root / self.date / "selected.json", {"date": self.date, "papers": []})
        write_json(self.root / self.date / "candidates.json", [])
        self.assertEqual(len(render.load_issue(self.cfg, self.date)[2]), 2)
        counts = {name: (candidates, detailed) for name, candidates, detailed, _
                  in notify.issue_counts(self.cfg, self.date)}
        self.assertEqual(counts["Graph Learning"], (1, 0))
        self.assertEqual(counts["Others"], (1, 0))
        write_json(self.root / self.date / ".rendered.json",
                   {"papers": 2, "summarized": 2, "problems": 0, "source_errors": []})
        self.assertFalse(autorun.issue_status(self.cfg, self.date)["complete"])

    def test_fetch_uses_saved_request_after_daily_rewrite(self):
        pid = "test:graph"
        self.prepare(pid, "Graph Transformer for Networks",
                     "We use message passing to train a graph model.")
        write_json(self.root / self.date / "selected.json", {"date": self.date, "papers": []})
        write_json(self.root / self.date / "candidates.json", [])
        with patch.object(fetch_papers, "load_config", return_value=self.cfg), \
             patch("probe_affiliations.probe_ids", return_value={}), \
             patch.object(sys, "argv", ["fetch_papers.py", "--date", self.date, "--ids", pid]), \
             redirect_stdout(io.StringIO()) as output:
            fetch_papers.main()
        self.assertEqual(json.loads(output.getvalue())[0]["id"], pid)
        pdir = self.root / self.date / "papers" / "test_graph"
        self.assertEqual(read_json(pdir / "meta.json")["selected_topics"], ["Graph Learning"])
        self.assertIn("We use message passing", (pdir / "content.md").read_text())
        self.assertEqual(read_json(self.root / self.date / "selected.json")["papers"][0]["id"], pid)

    def test_local_pdf_path_is_resolved_from_metadata_file(self):
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.7\n")
        record = self.root / "metadata.json"
        record.write_text(json.dumps({"id": "test:pdf", "title": "A PDF paper",
                                      "abstract": "A sample abstract.", "local_pdf": "paper.pdf"}))
        result = request_paper.prepare(self.cfg, self.date, None, str(record))
        saved = load_requested(self.cfg, self.date)["candidates"][result["id"]]
        self.assertEqual(saved["local_pdf"], str(source))

    def test_vote_up_replaces_prior_downvote_in_private_repo(self):
        (self.root / "token").write_text("test-token")
        old = {"name": f"{self.date}__test_graph.down.json", "sha": "old-sha"}
        api = Mock()
        api.get.return_value = SimpleNamespace(status_code=200, json=lambda: [old])
        api.put.return_value = SimpleNamespace(status_code=201)
        api.delete.return_value = SimpleNamespace(status_code=200)
        with patch.object(feedback, "session", return_value=api):
            result = feedback.vote_up(self.cfg, self.date, "test:graph")
        self.assertEqual(result, {"vote": "up", "created": True, "old_votes_removed": 1})
        put_url = api.put.call_args.args[0]
        self.assertTrue(put_url.endswith(f"/{self.date}__test_graph.up.json"))
        self.assertEqual(api.put.call_args.kwargs["json"]["message"],
                         "like reader-requested paper")
        self.assertTrue(api.delete.call_args.args[0].endswith(old["name"]))
        self.assertEqual(api.delete.call_args.kwargs["json"]["sha"], "old-sha")

    def test_requested_summary_does_not_use_normal_slot(self):
        regular = [{"id": f"regular-{i}", "topics": ["Graph Learning"], "summarize": True}
                   for i in range(3)]
        manual = {"id": "manual", "topics": ["Graph Learning"], "summarize": True, "requested": True}
        rows = regular + [manual]
        candidates = {p["id"]: {"id": p["id"], "authors": []} for p in rows}
        with patch("probe_affiliations.probe_ids", return_value={}):
            fetch_papers.assign_slots(self.cfg, self.root, {"papers": rows}, candidates)
        self.assertEqual(sum(p["summarize"] for p in regular), 2)
        self.assertTrue(manual["summarize"])
        entries = [{"sel": p, "meta": {"selected_topics": p["topics"]},
                    "vote": None, "prio": []} for p in rows]
        shown, others, _, _ = render.apply_display_caps(
            self.cfg, [e for e in entries if e["sel"]["summarize"]],
            [e for e in entries if not e["sel"]["summarize"]])
        self.assertEqual(len(shown), 3)
        self.assertEqual(len(others), 1)
        self.assertIn("manual", [e["sel"]["id"] for e in shown])

    def test_finish_validates_renders_and_requests_like(self):
        pid = "test:graph"
        self.prepare(pid, "Graph Transformer for Networks",
                     "We use message passing to train a graph model.")
        paper_dir = self.root / self.date / "papers" / "test_graph"
        paper_dir.mkdir(parents=True)
        en = {"tldr": "The paper studies graph representations.",
              "key_points": ["The method uses message passing."],
              "problem": "Graph models need useful node features.",
              "method": "The authors train a model on graph examples.",
              "results": "The model solves more test cases.",
              "why_it_matters": "Useful features can improve graph analysis.",
              "limitations": "The paper tests one model.",
              "relevance": "The study concerns graph representation learning."}
        ko = {"title": "그래프 표현 연구", "tldr": "이 논문은 graph representation을 연구합니다.",
              "key_points": ["이 방법은 message passing을 사용합니다."],
              "problem": "Graph model에는 유용한 node feature가 필요합니다.",
              "method": "저자들은 graph 예제로 model을 학습합니다.",
              "results": "이 model은 더 많은 문제를 해결합니다.",
              "why_it_matters": "유용한 feature는 graph 분석에 도움이 됩니다.",
              "limitations": "이 논문은 model 하나만 시험합니다.",
              "relevance": "이 연구는 graph representation learning과 관련됩니다."}
        write_json(paper_dir / "summary.json",
                   {"id": pid, "ste": ste_check.skill_receipt(), "en": en, "ko": ko, "figures": []})
        write_json(paper_dir / "assets.json", {"assets": []})
        calls = []

        def run(name, *args, timeout):
            calls.append(name)
            if name == "publish.py":
                return {"pushed": True}
            render.copy_assets(self.cfg)
            _, _, problems = render.render_issue_all(self.cfg, self.date)
            render.render_archive(self.cfg)
            return {self.date: {"problems": problems}}

        with patch.object(feedback, "vote_up", return_value={"vote": "up"}) as vote, \
             patch.object(feedback, "sync", return_value={"likes": 1}), \
             patch.object(request_paper, "run_script", side_effect=run):
            result = request_paper.finish(self.cfg, self.date, pid)
        vote.assert_called_once_with(self.cfg, self.date, pid)
        self.assertEqual(calls, ["render.py", "publish.py"])
        self.assertTrue(result["published"])
        self.assertIn("Graph Transformer for Networks", (self.root / self.date / "ko.html").read_text())


if __name__ == "__main__":
    unittest.main()

"""First-run choices and reconfiguration stay intact without touching the live config."""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup


class OnboardingTests(unittest.TestCase):
    def test_optional_features_survive_reconfiguration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yaml"
            answers = root / "answers.json"
            answers.write_text(json.dumps({
                "topics": [{"name": "Graph Learning", "description": "Graph representations.",
                            "keywords": ["graph neural network"]}],
                "schedule": {"enabled": True, "days": "daily", "times": ["10:30"]},
                "publish": {"enabled": True, "github_user": "octocat", "repo": "papers"},
                "feedback": {"enabled": True, "repo": "octocat/papers-feedback"},
                "notify": {"slack": {"enabled": True}},
            }))
            with patch.dict(os.environ, {"DAILY_PAPERS_CONFIG": str(config)}):
                with redirect_stdout(io.StringIO()) as preview:
                    setup.cmd_init(argparse.Namespace(answers=str(answers), dry_run=True,
                                                      force=False))
                self.assertFalse(config.exists())
                resolved = json.loads(preview.getvalue())["config"]
                self.assertTrue(resolved["feedback"]["enabled"])
                self.assertTrue(resolved["notify"]["slack"]["enabled"])
                with redirect_stdout(io.StringIO()):
                    setup.cmd_init(argparse.Namespace(answers=str(answers), dry_run=False,
                                                      force=False))
                answers.write_text(json.dumps({
                    "topics": [{"name": "Agents", "description": "Agent systems.",
                                "keywords": ["agentic workflow"]}]}))
                with redirect_stdout(io.StringIO()):
                    setup.cmd_init(argparse.Namespace(answers=str(answers), dry_run=False,
                                                      force=True))
            saved = yaml.safe_load(config.read_text())
            self.assertEqual(saved["topics"][0]["name"], "Agents")
            self.assertTrue(saved["schedule"]["enabled"])
            self.assertTrue(saved["publish"]["enabled"])
            self.assertEqual(saved["feedback"]["repo"], "octocat/papers-feedback")
            self.assertTrue(saved["notify"]["slack"]["enabled"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_upstream_github_updates.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_upstream_github_updates", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckUpstreamGitHubUpdatesTests(unittest.TestCase):
    def test_equal_adapted_body_is_not_reported_when_commit_sha_differs(self):
        module = load_module()
        skill = {
            "name": "demo",
            "repo": "owner/repo",
            "ref": "main",
            "sync_mode": "replace",
            "upstream_path": "skills/demo/SKILL.md",
            "last_synced_commit": "repository-head-checkpoint",
        }

        original_check = module.check_upstream_changes
        original_latest = module.github_latest_path_commit
        module.check_upstream_changes = lambda _skill, _token: {
            "changes": "none",
            "upstream_path": "skills/demo/SKILL.md",
        }
        module.github_latest_path_commit = lambda *_args: (
            "different-path-commit",
            "2026-08-18T00:00:00Z",
            None,
        )
        try:
            result = module.online_result(skill, token="token")
        finally:
            module.check_upstream_changes = original_check
            module.github_latest_path_commit = original_latest

        self.assertFalse(result["needs_update"])
        self.assertEqual("none", result["change_type"])
        self.assertEqual("different-path-commit", result["latest_commit"])

    def test_monitor_rollback_is_not_reported_as_update(self):
        module = load_module()
        skill = {
            "name": "curated",
            "repo": "owner/repo",
            "ref": "main",
            "sync_mode": "monitor",
            "last_synced_commit": "reviewed-checkpoint",
        }

        original_check = module.check_upstream_changes
        module.check_upstream_changes = lambda _skill, _token: {
            "changes": "upstream_rollback",
            "current_commit": "older-head",
        }
        try:
            result = module.online_result(skill, token="token")
        finally:
            module.check_upstream_changes = original_check

        self.assertFalse(result["needs_update"])
        self.assertEqual("upstream_rollback", result["change_type"])
        self.assertEqual("older-head", result["latest_commit"])

    def test_main_accepts_absolute_output_path(self):
        module = load_module()
        original_load = module.load_skills_with_upstream
        original_argv = sys.argv
        module.load_skills_with_upstream = lambda: []
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output = Path(tmpdir) / "upstream.json"
                sys.argv = [
                    "check_upstream_github_updates.py",
                    "--write-json",
                    str(output),
                ]
                self.assertEqual(1, module.main())
                payload = json.loads(output.read_text())
                self.assertEqual(0, payload["total_checked"])
                self.assertEqual("failed", payload["state"])
        finally:
            module.load_skills_with_upstream = original_load
            sys.argv = original_argv

    def test_summary_is_conservative_and_reports_tri_state(self):
        module = load_module()
        summary, state = module.summarize_states(
            [
                {"change_type": "none", "check_error": None},
                {"change_type": "artifact_changed", "check_error": None},
                {"change_type": "monitor_review", "check_error": None},
                {"change_type": "upstream_rollback", "check_error": None},
                {"change_type": "expected_skipped", "check_error": None},
            ]
        )
        self.assertEqual("degraded", state)
        self.assertEqual(5, summary["total"])
        self.assertEqual(
            summary["total"],
            sum(value for key, value in summary.items() if key != "total"),
        )

        _, state = module.summarize_states(
            [{"change_type": "none", "check_error": "metadata timeout"}]
        )
        self.assertEqual("failed", state)

    def test_offline_snapshot_is_never_reported_complete(self):
        module = load_module()
        summary, state = module.summarize_states(
            [{"change_type": "expected_skipped", "check_error": None}],
            online=False,
        )
        self.assertEqual("degraded", state)
        self.assertEqual(1, summary["expected_skipped"])

    def test_snapshot_returns_expected_skip_without_token_or_network(self):
        module = load_module()
        snapshot = {
            "name": "archived-demo",
            "kind": "snapshot",
            "sync_mode": "local-only",
            "repo": "owner/repo",
            "last_synced_commit": None,
            "local_path": REPO_ROOT
            / "skills"
            / "demo"
            / "SKILL.md",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "upstream.json"
            with (
                mock.patch.object(
                    module,
                    "load_skills_with_upstream",
                    return_value=[snapshot],
                ),
                mock.patch.object(
                    module,
                    "resolve_github_token",
                    side_effect=AssertionError("token lookup must not run"),
                ),
                mock.patch.object(
                    module,
                    "check_upstream_changes",
                    side_effect=AssertionError("network check must not run"),
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "check_upstream_github_updates.py",
                        "--online",
                        "--write-json",
                        str(output),
                    ],
                ),
            ):
                self.assertEqual(0, module.main())
            payload = json.loads(output.read_text())
            self.assertEqual(
                "expected_skipped",
                payload["rows"][0]["change_type"],
            )
            self.assertEqual(1, payload["summary"]["expected_skipped"])

    def test_per_skill_exception_becomes_unavailable_and_report_is_atomic(self):
        module = load_module()
        root = REPO_ROOT
        skill = {
            "name": "demo",
            "repo": "owner/repo",
            "ref": "main",
            "sync_mode": "monitor",
            "upstream_path": "skills/demo/SKILL.md",
            "last_synced_commit": "a" * 40,
            "local_path": root / "skills" / "demo" / "SKILL.md",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "upstream.json"
            with (
                mock.patch.object(
                    module, "load_skills_with_upstream", return_value=[skill]
                ),
                mock.patch.object(
                    module,
                    "resolve_github_token",
                    return_value="token",
                ),
                mock.patch.object(
                    module,
                    "online_result",
                    side_effect=RuntimeError("network exploded"),
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "check_upstream_github_updates.py",
                        "--online",
                        "--write-json",
                        str(output),
                    ],
                ),
            ):
                self.assertEqual(1, module.main())
            payload = json.loads(output.read_text())
            self.assertEqual("failed", payload["state"])
            self.assertEqual(1, payload["summary"]["unavailable"])
            self.assertIn("network exploded", payload["rows"][0]["check_error"])
            self.assertEqual(
                payload["summary"]["total"],
                sum(
                    payload["summary"][key]
                    for key in module.BUCKETS
                ),
            )
            self.assertEqual([], list(Path(tmpdir).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()

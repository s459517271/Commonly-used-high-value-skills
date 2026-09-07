from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_sync_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("generate_sync_report", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GenerateSyncReportTests(unittest.TestCase):
    def valid_discovery(self) -> dict:
        return {
            "local_skill_count": 1,
            "raw_discovered": 0,
            "total_discovered": 0,
            "unique_discovered": 0,
            "discoveries": [],
            "source_health": {
                source: {
                    "status": "healthy",
                    "queries": 1,
                    "results": 0,
                    "raw_results": 0,
                    "emitted": 0,
                    "unique_emitted": 0,
                    "errors": [],
                }
                for source in ("github", "skills_sh", "clawhub")
            },
            "errors": [],
        }

    def valid_upstream(self) -> dict:
        return {
            "mode": "online",
            "state": "complete",
            "summary": {
                "total": 1,
                "equal": 1,
                "changed": 0,
                "monitor_review": 0,
                "unavailable": 0,
                "rollback": 0,
                "expected_skipped": 0,
            },
            "total_checked": 1,
            "needs_update_count": 0,
            "needs_review_count": 0,
            "check_error_count": 0,
            "rows": [
                {
                    "change_type": "none",
                    "check_error": None,
                    "needs_update": False,
                    "needs_review": False,
                }
            ],
        }

    def test_source_label_uses_discovery_url_without_blank_github_repo(self) -> None:
        module = load_module()

        label = module.source_label(
            {
                "source": "skills.sh (supabase/agent-skills)",
                "url": "https://skills.sh/supabase/agent-skills/supabase",
            }
        )

        self.assertEqual("[supabase/agent-skills](https://skills.sh/supabase/agent-skills/supabase)", label)

    def test_upstream_skill_name_falls_back_to_video_name(self) -> None:
        module = load_module()

        self.assertEqual(
            "supabase-postgres-best-practices",
            module.upstream_skill_name({"video_name": "supabase-postgres-best-practices"}),
        )

    def test_tri_state_combines_source_health_and_upstream_state(self) -> None:
        module = load_module()
        self.assertEqual(
            "complete",
            module.discovery_state(
                self.valid_discovery(),
                exists=True,
            ),
        )
        self.assertEqual(
            "degraded",
            module.combine_state("complete", "degraded"),
        )
        self.assertEqual(
            "failed",
            module.combine_state("degraded", "failed"),
        )
        self.assertEqual(
            "failed",
            module.upstream_report_state({"state": "complete"}, exists=True),
        )

    def test_main_generates_non_empty_skill_and_source_fields(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            tmp_dir = Path(tmp)
            discovery = tmp_dir / "discovery.json"
            upstream = tmp_dir / "upstream.json"
            output = tmp_dir / "sync-report.md"
            discovery.write_text(
                json.dumps(
                    {
                        "local_skill_count": 1,
                        "raw_discovered": 1,
                        "total_discovered": 1,
                        "unique_discovered": 1,
                        "source_health": {
                            source: {
                                "status": "healthy",
                                "queries": 1,
                                "results": 1 if source == "skills_sh" else 0,
                                "raw_results": 1 if source == "skills_sh" else 0,
                                "emitted": 1 if source == "skills_sh" else 0,
                                "unique_emitted": (
                                    1 if source == "skills_sh" else 0
                                ),
                                "errors": [],
                            }
                            for source in ("github", "skills_sh", "clawhub")
                        },
                        "errors": [],
                        "discoveries": [
                            {
                                "source_key": "skills_sh",
                                "name": "supabase",
                                "source": "skills.sh (supabase/agent-skills)",
                                "url": "https://skills.sh/supabase/agent-skills/supabase",
                                "repo_stars": 100,
                                "description": "Supabase workflow guidance",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            upstream.write_text(
                json.dumps(
                    {
                        "mode": "online",
                        "state": "complete",
                        "summary": {
                            "total": 1,
                            "equal": 0,
                            "changed": 1,
                            "monitor_review": 0,
                            "unavailable": 0,
                            "rollback": 0,
                            "expected_skipped": 0,
                        },
                        "total_checked": 1,
                        "needs_update_count": 1,
                        "needs_review_count": 0,
                        "check_error_count": 0,
                        "rows": [
                            {
                                "video_name": "graphify",
                                "change_type": "artifact_changed",
                                "check_error": None,
                                "needs_update": True,
                                "needs_review": False,
                                "latest_commit": "1234567890abcdef",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            old_cwd = os.getcwd()
            try:
                os.chdir(REPO_ROOT)
                module = load_module()
                module.main_args = None
                import sys

                old_argv = sys.argv
                sys.argv = [
                    "generate_sync_report.py",
                    "--discovery",
                    str(discovery.relative_to(REPO_ROOT)),
                    "--upstream",
                    str(upstream.relative_to(REPO_ROOT)),
                    "--output",
                    str(output.relative_to(REPO_ROOT)),
                ]
                self.assertEqual(0, module.main())
            finally:
                sys.argv = old_argv
                os.chdir(old_cwd)

            report = output.read_text(encoding="utf-8")
            self.assertIn("**Automation state**: `complete`", report)
            self.assertIn("[supabase/agent-skills](https://skills.sh/supabase/agent-skills/supabase)", report)
            self.assertIn("**graphify**", report)
            self.assertNotIn("https://github.com/)", report)
            self.assertNotIn("****", report)

    def test_discovery_requires_exact_three_sources_and_consistent_status(self):
        module = load_module()
        payload = self.valid_discovery()
        payload["source_health"].pop("clawhub")
        with self.assertRaisesRegex(ValueError, "exactly"):
            module.validate_discovery(payload)

        payload = self.valid_discovery()
        payload["source_health"]["github"]["queries"] = True
        with self.assertRaisesRegex(ValueError, "non-negative integers"):
            module.validate_discovery(payload)

        payload = self.valid_discovery()
        payload["source_health"]["github"].update(
            {
                "status": "healthy",
                "errors": [
                    {
                        "kind": "network_error",
                        "status_code": None,
                        "message": "timeout",
                    }
                ],
            }
        )
        payload["errors"] = [
            {
                "kind": "network_error",
                "status_code": None,
                "message": "timeout",
                "source": "github",
            }
        ]
        with self.assertRaisesRegex(ValueError, "disagrees"):
            module.validate_discovery(payload)

    def test_upstream_recomputes_rows_and_rejects_declared_mismatch(self):
        module = load_module()
        payload = self.valid_upstream()
        payload["summary"]["equal"] = True
        with self.assertRaisesRegex(ValueError, "disagrees"):
            module.validate_upstream(payload)

        payload = self.valid_upstream()
        payload["state"] = "degraded"
        with self.assertRaisesRegex(ValueError, "declared state"):
            module.validate_upstream(payload)

        payload = self.valid_upstream()
        payload["rows"][0]["needs_update"] = True
        with self.assertRaisesRegex(ValueError, "needs_update"):
            module.validate_upstream(payload)

    def test_invalid_inputs_still_write_failed_github_outputs(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            discovery = root / "discovery.json"
            upstream = root / "upstream.json"
            output = root / "report.md"
            github_output = root / "github-output"
            discovery.write_text("{}", encoding="utf-8")
            upstream.write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"GITHUB_OUTPUT": str(github_output)},
                clear=False,
            ):
                result = module.main(
                    [
                        "--discovery",
                        str(discovery),
                        "--upstream",
                        str(upstream),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(1, result)
            self.assertIn("sync_state=failed", github_output.read_text())
            self.assertIn("needs_attention=true", github_output.read_text())
            self.assertIn("Report Validation Failures", output.read_text())

    def test_outputs_are_not_published_before_report_write_succeeds(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            discovery = root / "discovery.json"
            upstream = root / "upstream.json"
            github_output = root / "github-output"
            discovery.write_text(
                json.dumps(self.valid_discovery()),
                encoding="utf-8",
            )
            upstream.write_text(
                json.dumps(self.valid_upstream()),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"GITHUB_OUTPUT": str(github_output)},
                    clear=False,
                ),
                mock.patch.object(
                    module,
                    "_atomic_write_text",
                    side_effect=OSError("disk full"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    module.main(
                        [
                            "--discovery",
                            str(discovery),
                            "--upstream",
                            str(upstream),
                            "--output",
                            str(root / "report.md"),
                        ]
                    )
            self.assertFalse(github_output.exists())

    def test_discovery_rows_and_source_conservation_are_strict(self):
        module = load_module()
        payload = self.valid_discovery()
        payload["source_health"]["github"].update(
            {
                "results": 1,
                "raw_results": 1,
                "emitted": 1,
                "unique_emitted": 1,
            }
        )
        payload.update(
            {
                "raw_discovered": 1,
                "total_discovered": 1,
                "unique_discovered": 1,
                "discoveries": [
                    {
                        "source_key": "github",
                        "name": "demo",
                        "source": "GitHub",
                        "url": "https://github.com/example/demo",
                        "repo_stars": 1,
                        "description": "Demo",
                    }
                ],
            }
        )
        self.assertEqual("complete", module.validate_discovery(payload))
        payload["discoveries"][0].pop("source_key")
        with self.assertRaisesRegex(ValueError, "source_key"):
            module.validate_discovery(payload)

    def test_discovery_errors_are_rendered_in_stable_source_order(self):
        module = load_module()
        discovery = self.valid_discovery()
        discovery["source_health"]["skills_sh"].update(
            {
                "status": "unavailable",
                "errors": [
                    {
                        "kind": "network_error",
                        "status_code": None,
                        "message": "timeout",
                    }
                ],
            }
        )
        discovery["errors"] = [
            {
                "kind": "network_error",
                "status_code": None,
                "message": "timeout",
                "source": "skills_sh",
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            discovery_path = root / "discovery.json"
            upstream_path = root / "upstream.json"
            output = root / "report.md"
            discovery_path.write_text(json.dumps(discovery), encoding="utf-8")
            upstream_path.write_text(
                json.dumps(self.valid_upstream()), encoding="utf-8"
            )
            self.assertEqual(
                0,
                module.main(
                    [
                        "--discovery",
                        str(discovery_path),
                        "--upstream",
                        str(upstream_path),
                        "--output",
                        str(output),
                    ]
                ),
            )
            report = output.read_text(encoding="utf-8")
            self.assertIn("Discovery Source Errors (1)", report)
            self.assertIn("**skills_sh** (`network_error`)", report)


if __name__ == "__main__":
    unittest.main()

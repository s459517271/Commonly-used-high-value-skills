from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_repo_health_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("generate_repo_health_report", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


class GenerateRepoHealthReportTests(unittest.TestCase):
    def test_default_report_uses_live_refresh_queue_not_stale_artifact(self):
        module = load_module()
        with mock.patch.object(module, "build_queue", return_value=[]):
            queue = module.load_refresh_queue(REPO_ROOT, module.DEFAULT_REPORTS_DIR)
        self.assertEqual([], queue)

    def test_repo_health_report_generation(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            tmp_dir = Path(tmp)
            reports_dir = tmp_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            json_output = tmp_dir / "repo-health.json"
            md_output = tmp_dir / "repo-health.md"

            result = run_script(
                "--output-json",
                str(json_output),
                "--output-md",
                str(md_output),
                "--reports-dir",
                str(reports_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json_output.exists())
            self.assertTrue(md_output.exists())

            payload = json.loads(json_output.read_text(encoding="utf-8"))
            markdown = md_output.read_text(encoding="utf-8")

            self.assertIn("skills_total", payload)
            self.assertIn("license_audit", payload)
            self.assertIn("dead_links", payload)
            self.assertIn("refresh_queue", payload)
            self.assertIn("# Repo Health Report", markdown)
            self.assertIn("## License Audit", markdown)


if __name__ == "__main__":
    unittest.main()

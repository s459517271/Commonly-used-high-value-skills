import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_skill_portfolio.py"


def load_module():
    spec = importlib.util.spec_from_file_location("audit_skill_portfolio", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def skill_text(name: str, description: str) -> str:
    body = "\n".join(
        [
            "---",
            f"name: {name}",
            f"description: '{description}'",
            'zh_description: "测试技能。"',
            'version: "1.0.0"',
            "source: in-house",
            'tags: \'["test"]\'',
            "quality: 3",
            "---",
            "",
            f"# {name}",
            "",
            "## Workflow",
            "",
            "Inspect the input and choose a bounded action.",
            "",
            "## Commands",
            "",
            "```bash",
            "echo verify",
            "```",
            "",
            "## Validation",
            "",
            "Run the command and inspect the result.",
            "",
            "## Boundaries",
            "",
            "Do not mutate external systems without authorization.",
        ]
        + ["Additional concrete guidance for the workflow."] * 70
    )
    return body + "\n"


class AuditSkillPortfolioTests(unittest.TestCase):
    def test_policy_violation_detects_retired_skill(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "skills" / "category" / "legacy-helper"
            skill_dir.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(
                skill_text(
                    "legacy-helper",
                    "Use when a user asks for the legacy helper workflow and validation.",
                ),
                encoding="utf-8",
            )
            audit = module.audit_skill(skill_md)
            violations = module.policy_violations(
                [audit],
                {
                    "retired_skills": [
                        {
                            "name": "legacy-helper",
                            "replacement": "canonical-helper",
                            "reason": "duplicate",
                        }
                    ]
                },
            )

            self.assertEqual(1, len(violations))
            self.assertEqual("canonical-helper", violations[0]["replacement"])

    def test_cli_check_policy_returns_nonzero_for_reintroduced_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "skills" / "category" / "retired-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                skill_text(
                    "retired-skill",
                    "Use when a user asks for a retired test workflow.",
                ),
                encoding="utf-8",
            )
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "retired_skills": [
                            {
                                "name": "retired-skill",
                                "replacement": "current-skill",
                                "reason": "covered",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--skills-dir",
                    str(root / "skills"),
                    "--policy",
                    str(policy),
                    "--json-output",
                    str(root / "audit.json"),
                    "--markdown-output",
                    str(root / "audit.md"),
                    "--check-policy",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=REPO_ROOT,
            )

            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("retired-skill -> current-skill", result.stdout)

    def test_audit_counts_reusable_assets(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "skills" / "category" / "asset-skill"
            (skill_dir / "scripts").mkdir(parents=True)
            (skill_dir / "references").mkdir()
            (skill_dir / "assets").mkdir()
            (skill_dir / "SKILL.md").write_text(
                skill_text(
                    "asset-skill",
                    "Use when a user needs a script-backed artifact workflow.",
                ),
                encoding="utf-8",
            )
            (skill_dir / "scripts" / "verify.py").write_text("print('ok')\n", encoding="utf-8")
            (skill_dir / "references" / "contract.md").write_text("# Contract\n", encoding="utf-8")
            (skill_dir / "assets" / "template.txt").write_text("template\n", encoding="utf-8")

            audit = module.audit_skill(skill_dir / "SKILL.md")

            self.assertEqual(4, audit.file_count)
            self.assertEqual(1, audit.script_count)
            self.assertEqual(1, audit.reference_count)
            self.assertEqual(1, audit.asset_count)


if __name__ == "__main__":
    unittest.main()

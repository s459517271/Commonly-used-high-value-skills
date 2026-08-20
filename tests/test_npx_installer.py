import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "bin" / "install-skills.js"
PACKAGE_JSON = REPO_ROOT / "package.json"


class NpxInstallerTests(unittest.TestCase):
    def test_package_exposes_installer_bin_and_skill_files(self):
        data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))

        self.assertEqual("common-high-value-skills", data["name"])
        self.assertEqual("2.0.0", data["version"])
        self.assertEqual("bin/install-skills.js", data["bin"]["high-value-skills"])
        self.assertIn("skills/", data["files"])
        self.assertIn(
            "docs/sources/open-gsd-core-2026-08.bundle.json", data["files"]
        )
        self.assertNotIn("openclaw-skills/", data["files"])

    def test_installer_help_and_target_listing_work(self):
        help_result = subprocess.run(
            ["node", str(INSTALLER), "--help"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("high-value-skills install", help_result.stdout)

        targets_result = subprocess.run(
            ["node", str(INSTALLER), "list-targets"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("agents-project", targets_result.stdout)
        self.assertIn("codex", targets_result.stdout)
        self.assertIn("openclaw", targets_result.stdout)

        bundles_result = subprocess.run(
            ["node", str(INSTALLER), "list-bundles"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("gsd-core", bundles_result.stdout)
        self.assertIn("gsd-pi", bundles_result.stdout)
        self.assertIn("optional/list-only", bundles_result.stdout)

    def test_installer_flattens_categorized_skills_to_custom_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "developer-engineering" / "sample-skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: test skill\n---\n\n# Sample\n",
                encoding="utf-8",
            )
            (source / "references").mkdir()
            (source / "references" / "note.md").write_text("reference\n", encoding="utf-8")
            dest = root / "installed"

            result = subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "install",
                    "--target",
                    "custom",
                    "--source-root",
                    str(root / "source"),
                    "--dir",
                    str(dest),
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )

            self.assertIn("Installed 1 skills", result.stdout)
            self.assertTrue((dest / "sample-skill" / "SKILL.md").exists())
            self.assertTrue((dest / "sample-skill" / "references" / "note.md").exists())
            manifest = json.loads(
                (dest / ".high-value-skills-manifest.json").read_text(encoding="utf-8")
            )
            entry = manifest["skills"]["sample-skill"]
            self.assertEqual("sample-skill", entry["owner"])
            self.assertRegex(entry["source_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                {"path", "sha256", "mode", "owner"}, set(entry["files"][0])
            )

    def test_installer_filters_by_category_and_skill_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for category, name in [
                ("developer-engineering", "api-helper"),
                ("developer-engineering", "db-helper"),
                ("security-and-reliability", "security-helper"),
            ]:
                skill = root / "source" / category / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test skill\n---\n\n# Test\n",
                    encoding="utf-8",
                )

            dest = root / "installed"
            result = subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "install",
                    "--target",
                    "custom",
                    "--source-root",
                    str(root / "source"),
                    "--dir",
                    str(dest),
                    "--category",
                    "developer-engineering",
                    "--skill",
                    "db-helper",
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )

            self.assertIn("Installed 1 skills", result.stdout)
            self.assertTrue((dest / "db-helper" / "SKILL.md").exists())
            self.assertFalse((dest / "api-helper").exists())
            self.assertFalse((dest / "security-helper").exists())

            listed = subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "list-skills",
                    "--source-root",
                    str(root / "source"),
                    "--category",
                    "security-and-reliability",
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("security-helper", listed.stdout)
            self.assertNotIn("api-helper", listed.stdout)

    def test_dry_run_is_zero_write_and_reports_idempotent_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "developer-engineering" / "mode-skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# mode\n", encoding="utf-8")
            script = source / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            dest = root / "installed"
            command = [
                "node",
                str(INSTALLER),
                "install",
                "--target",
                "custom",
                "--source-root",
                str(root / "source"),
                "--dir",
                str(dest),
            ]
            subprocess.run(command, cwd=REPO_ROOT, check=True, capture_output=True)
            manifest_path = dest / ".high-value-skills-manifest.json"
            before = {
                path.relative_to(root).as_posix(): (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in root.rglob("*")
                if path.is_file()
            }

            result = subprocess.run(
                [*command, "--dry-run"],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            after = {
                path.relative_to(root).as_posix(): (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    path.stat().st_mtime_ns,
                )
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertIn("Updated: 0, Unchanged: 1", result.stdout)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            modes = {
                item["path"]: item["mode"]
                for item in manifest["skills"]["mode-skill"]["files"]
            }
            self.assertEqual("755", modes["run.sh"])

            missing_dest = root / "never-created"
            subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "install",
                    "--target",
                    "custom",
                    "--source-root",
                    str(root / "source"),
                    "--dir",
                    str(missing_dest),
                    "--dry-run",
                ],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            self.assertFalse(missing_dest.exists())

    def test_prune_deletes_unchanged_owned_and_archives_modified_or_unowned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            for name in ("keeper", "retired-clean", "retired-modified"):
                skill = source_root / "operations-general" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            dest = root / "installed"
            base = [
                "node",
                str(INSTALLER),
                "install",
                "--target",
                "custom",
                "--source-root",
                str(source_root),
                "--dir",
                str(dest),
            ]
            subprocess.run(base, cwd=REPO_ROOT, check=True, capture_output=True)
            (dest / "retired-modified" / "SKILL.md").write_text(
                "# user edit\n", encoding="utf-8"
            )
            unowned = dest / "retired-unowned"
            unowned.mkdir()
            (unowned / "SKILL.md").write_text("# local\n", encoding="utf-8")
            broken_link = dest / "retired-link"
            broken_link.symlink_to(root / "missing-local-target", target_is_directory=True)
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "retired_skills": [
                            {"name": "retired-clean"},
                            {"name": "retired-modified"},
                            {"name": "retired-unowned"},
                            {"name": "retired-link"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    *base,
                    "--skill",
                    "keeper",
                    "--prune-retired",
                    "--portfolio-policy",
                    str(policy),
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("delete 1, archive 3", result.stdout)
            for name in (
                "retired-clean",
                "retired-modified",
                "retired-unowned",
                "retired-link",
            ):
                self.assertFalse((dest / name).exists())
            backups = root / ".high-value-skills-backups"
            self.assertEqual(
                "# user edit\n",
                next(backups.glob("*/installed/retired-modified/SKILL.md")).read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue(
                next(backups.glob("*/installed/retired-unowned/SKILL.md")).exists()
            )
            self.assertTrue(
                next(backups.glob("*/installed/retired-link")).is_symlink()
            )

    def test_bundle_dry_run_is_pinned_and_never_writes_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {**os.environ, "HOME": str(home)}
            before = list(home.rglob("*"))
            result = subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "install",
                    "--bundle",
                    "gsd-core",
                    "--target",
                    "codex",
                    "--dry-run",
                ],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(before, list(home.rglob("*")))
            self.assertIn("@opengsd/gsd-core@1.11.0", result.stdout)
            self.assertIn("71 skills, 34 agents, 71 commands", result.stdout)

            rejected = subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "install",
                    "--bundle",
                    "gsd-pi",
                    "--target",
                    "codex",
                    "--dry-run",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn("optional/list-only", rejected.stderr)

    def test_conflict_audit_reports_cross_root_digest_and_ownership_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / "operations-general" / "shared-skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# shared\n", encoding="utf-8")
            roots = [root / "one", root / "two"]
            for dest in roots:
                subprocess.run(
                    [
                        "node",
                        str(INSTALLER),
                        "install",
                        "--target",
                        "custom",
                        "--source-root",
                        str(root / "source"),
                        "--dir",
                        str(dest),
                    ],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                )
            (roots[1] / "shared-skill" / "SKILL.md").write_text(
                "# drift\n", encoding="utf-8"
            )
            result = subprocess.run(
                [
                    "node",
                    str(INSTALLER),
                    "audit-conflicts",
                    "--roots",
                    ",".join(map(str, roots)),
                    "--json",
                ],
                cwd=REPO_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            data = json.loads(result.stdout)
            self.assertEqual(1, data["duplicate_names"])
            self.assertEqual(1, data["content_conflicts"])
            ownership = {entry["ownership"] for entry in data["matrix"][0]["entries"]}
            self.assertEqual(
                {"common-high-value-skills", "unowned-or-modified"}, ownership
            )


if __name__ == "__main__":
    unittest.main()

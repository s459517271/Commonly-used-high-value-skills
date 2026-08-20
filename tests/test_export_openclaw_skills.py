import importlib.util
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "export_openclaw_skills.py"


def load_export_module():
    spec = importlib.util.spec_from_file_location("export_openclaw_skills", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load exporter from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportOpenClawSkillsTests(unittest.TestCase):
    @staticmethod
    def git_index_modes(*paths: str) -> dict[str, str]:
        output = subprocess.check_output(
            [
                "git",
                "ls-files",
                "--stage",
                "-z",
                "--",
                *paths,
            ],
            cwd=REPO_ROOT,
        )
        modes: dict[str, str] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            metadata, separator, raw_path = record.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or fields[2] != b"0":
                raise AssertionError(f"malformed Git index record: {record!r}")
            modes[raw_path.decode("utf-8")] = fields[0].decode("ascii")
        return modes

    def test_export_flattens_skill_tree_and_synthesizes_frontmatter(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"

            skill_dir = source_root / "developer-engineering" / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "notes.txt").write_text("supporting text\n", encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    # Demo Skill

                    Automates demo workflow for local testing.

                    ## Usage

                    Use this skill when a demo task needs repeatable setup.
                    """
                ),
                encoding="utf-8",
            )

            exported = module.export_openclaw_skills(source_root, output_root)

            self.assertEqual(["demo-skill"], [item.name for item in exported])
            exported_skill = output_root / "demo-skill"
            self.assertTrue((exported_skill / "notes.txt").exists())

            exported_skill_md = (exported_skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(exported_skill_md.startswith("---\n"))
            self.assertIn("name: demo-skill", exported_skill_md)
            self.assertIn("description: Automates demo workflow for local testing.", exported_skill_md)
            self.assertIn("# Demo Skill", exported_skill_md)

    def test_export_preserves_executable_mode(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"
            skill_dir = source_root / "developer-engineering" / "demo-skill"
            scripts = skill_dir / "scripts"
            scripts.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(
                "---\nname: demo-skill\ndescription: Demo.\n---\n# Demo\n",
                encoding="utf-8",
            )
            skill_md.chmod(0o755)
            executable = scripts / "check.sh"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)

            module.export_openclaw_skills(source_root, output_root)

            exported = output_root / "demo-skill" / "scripts" / "check.sh"
            self.assertEqual(executable.read_bytes(), exported.read_bytes())
            self.assertEqual(0o755, stat.S_IMODE(exported.stat().st_mode))
            exported_skill = output_root / "demo-skill" / "SKILL.md"
            self.assertEqual(0o755, stat.S_IMODE(exported_skill.stat().st_mode))

    def test_tracked_openclaw_export_modes_match_canonical_index(self):
        module = load_export_module()
        canonical_modes = self.git_index_modes("skills")
        exported_modes = self.git_index_modes("openclaw-skills")
        mismatches: list[str] = []

        for canonical, mode in sorted(canonical_modes.items()):
            parts = Path(canonical).parts
            if len(parts) < 4:
                continue
            skill_root = Path(*parts[:3])
            if f"{skill_root.as_posix()}/SKILL.md" not in canonical_modes:
                continue
            relative = Path(*parts[3:])
            if any(part in module.IGNORED_NAMES for part in relative.parts):
                continue
            exported = (
                Path("openclaw-skills") / parts[2] / relative
            ).as_posix()
            exported_mode = exported_modes.get(exported)
            if exported_mode != mode:
                mismatches.append(
                    f"{canonical} -> {exported}: {mode} != {exported_mode}"
                )

        self.assertEqual(
            [],
            mismatches,
            "Tracked OpenClaw artifacts must preserve canonical Git modes.",
        )

    def test_export_preserves_extra_frontmatter_blocks(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"

            skill_dir = source_root / "knowledge-and-pm-integrations" / "metadata-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: metadata-skill
                    description: |
                      Manage remote systems with extra context.
                    metadata:
                      openclaw:
                        requires:
                          env:
                            - API_KEY
                    ---

                    # Metadata Skill

                    Example body.
                    """
                ),
                encoding="utf-8",
            )

            module.export_openclaw_skills(source_root, output_root)

            exported_skill_md = (output_root / "metadata-skill" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("name: metadata-skill", exported_skill_md)
            self.assertIn("description: Manage remote systems with extra context.", exported_skill_md)
            self.assertIn("metadata:", exported_skill_md)
            self.assertIn("API_KEY", exported_skill_md)
            self.assertIn("# Metadata Skill", exported_skill_md)

    def test_export_quotes_yaml_unsafe_scalars(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"

            api_skill = source_root / "developer-engineering" / "api-design-reviewer"
            api_skill.mkdir(parents=True)
            (api_skill / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    # API Design Reviewer

                    **Maintainer:** Claude Skills Team

                    Review API designs for consistency.
                    """
                ),
                encoding="utf-8",
            )

            competitor_skill = source_root / "operations-general" / "competitors-analysis"
            competitor_skill.mkdir(parents=True)
            (competitor_skill / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: competitors-analysis
                    description: 'Analyze competitor repositories with evidence-based approach.'
                    context: fork
                    agent: general-purpose
                    argument-hint: [product-name] [competitor-url]
                    ---

                    # Competitors Analysis

                    Compare products with repository evidence.
                    """
                ),
                encoding="utf-8",
            )

            module.export_openclaw_skills(source_root, output_root)

            api_text = (output_root / "api-design-reviewer" / "SKILL.md").read_text(encoding="utf-8")
            competitor_text = (output_root / "competitors-analysis" / "SKILL.md").read_text(
                encoding="utf-8"
            )

            self.assertIn("description: 'Maintainer: Claude Skills Team.'", api_text)
            self.assertIn("argument-hint: '[product-name] [competitor-url]'", competitor_text)

    def test_export_forces_name_to_match_directory_for_openclaw_compatibility(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"

            skill_dir = source_root / "task-understanding-decomposition" / "reflect-learn"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: reflect
                    description: Learn from conversation corrections.
                    ---

                    # Reflect Learn
                    """
                ),
                encoding="utf-8",
            )

            module.export_openclaw_skills(source_root, output_root)

            exported_skill_md = (output_root / "reflect-learn" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("name: reflect-learn", exported_skill_md)

    def test_export_normalizes_nested_sample_skill_markdown(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"

            skill_dir = source_root / "developer-engineering" / "skill-tester"
            nested = skill_dir / "assets" / "sample-skill"
            nested.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: skill-tester
                    description: Validate skills.
                    ---

                    # Skill Tester
                    """
                ),
                encoding="utf-8",
            )
            (nested / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    # Sample Skill

                    Broken nested skill example.
                    """
                ),
                encoding="utf-8",
            )

            module.export_openclaw_skills(source_root, output_root)

            nested_text = (output_root / "skill-tester" / "assets" / "sample-skill" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertTrue(nested_text.startswith("---\n"))
            self.assertIn("name: sample-skill", nested_text)

    def test_export_converts_stringified_metadata_to_yaml_mapping(self):
        module = load_export_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_root = tmp / "skills"
            output_root = tmp / "openclaw-skills"

            skill_dir = source_root / "developer-engineering" / "git-essentials"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: git-essentials
                    description: Essential Git commands.
                    metadata: '{"clawdbot":{"emoji":"🌳","requires":{"bins":["git"]}}}'
                    ---

                    # Git Essentials
                    """
                ),
                encoding="utf-8",
            )

            module.export_openclaw_skills(source_root, output_root)

            exported_text = (output_root / "git-essentials" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("metadata:", exported_text)
            self.assertIn("clawdbot:", exported_text)
            self.assertNotIn("metadata: '{", exported_text)


if __name__ == "__main__":
    unittest.main()

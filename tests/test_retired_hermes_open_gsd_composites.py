import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RETIRED = {
    "hermes-graphify-gsd-nonintrusive-workflow",
    "hermes-graphify-gsd-runtime-operator",
    "hermes-graphify-gsd-project-integration",
    "gsd-graphify-brownfield-bootstrap",
}
ROUTER = "hermes-open-gsd-workflow"
MIGRATION = "open-gsd-core-migration"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RetiredHermesOpenGsdCompositeTests(unittest.TestCase):
    def test_retired_source_directories_are_absent_and_replacements_exist(self):
        discovered = {
            path.parent.name for path in REPO_ROOT.glob("skills/*/*/SKILL.md")
        }

        self.assertTrue(RETIRED.isdisjoint(discovered))
        self.assertIn(ROUTER, discovered)
        self.assertIn(MIGRATION, discovered)

    def test_openclaw_export_cannot_rediscover_retired_aliases(self):
        exporter = load_module(
            "retired_composite_exporter",
            REPO_ROOT / "scripts" / "export_openclaw_skills.py",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "openclaw-skills"
            exported = exporter.export_openclaw_skills(
                REPO_ROOT / "skills",
                output_root,
            )
            names = {path.name for path in exported}

        self.assertTrue(RETIRED.isdisjoint(names))
        self.assertIn(ROUTER, names)
        self.assertIn(MIGRATION, names)

    def test_portfolio_policy_has_permanent_tombstones_and_aliases(self):
        policy = json.loads(
            (REPO_ROOT / "docs" / "sources" / "portfolio-policy.json").read_text(
                encoding="utf-8"
            )
        )
        retired = {
            entry["name"]: entry
            for entry in policy["retired_skills"]
            if entry.get("name") in RETIRED
        }
        self.assertEqual(RETIRED, set(retired))
        for entry in retired.values():
            self.assertEqual(ROUTER, entry["replacement"])
            self.assertEqual("permanent", entry["tombstone"])

        router_group = next(
            group
            for group in policy["canonical_groups"]
            if group.get("canonical") == ROUTER
        )
        self.assertEqual(RETIRED, set(router_group["retired_aliases"]))

    def test_provenance_locks_router_and_migration_dependencies(self):
        payload = json.loads(
            (REPO_ROOT / "docs" / "sources" / "in-house.skills.json").read_text(
                encoding="utf-8"
            )
        )
        entries = {
            entry["normalized_slug"]: entry
            for entry in payload["skills"]
            if entry.get("normalized_slug") in {ROUTER, MIGRATION}
        }
        self.assertEqual({ROUTER, MIGRATION}, set(entries))

        router = entries[ROUTER]
        self.assertEqual("composite", router["kind"])
        router_dependencies = {
            dependency.get("skill") or dependency.get("source_package")
            for dependency in router["composition"]["depends_on"]
        }
        self.assertEqual(
            {
                "hermes-agent",
                "graphify",
                "open-gsd/gsd-core",
                "open-gsd/gsd-pi",
            },
            router_dependencies,
        )
        self.assertEqual(
            router_dependencies,
            set(router["composition"]["dependency_lock"]),
        )

        migration = entries[MIGRATION]
        self.assertEqual("composite", migration["kind"])
        self.assertEqual(
            [{"source_package": "open-gsd/gsd-core", "role": "migration-target"}],
            migration["composition"]["depends_on"],
        )
        self.assertEqual(
            {"open-gsd/gsd-core"},
            set(migration["composition"]["dependency_lock"]),
        )

    def test_category_readme_generator_has_no_retired_usage_state_machine(self):
        generator = (
            REPO_ROOT / "scripts" / "generate_category_readmes.py"
        ).read_text(encoding="utf-8")
        for retired in RETIRED:
            self.assertNotIn(retired, generator)
        for obsolete_state in (
            "writer lease",
            "auto-continue",
            "task-board",
            "install-hermes-auto-continue-cron",
            "gsd-sdk init",
        ):
            self.assertNotIn(obsolete_state, generator)


if __name__ == "__main__":
    unittest.main()

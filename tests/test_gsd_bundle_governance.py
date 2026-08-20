import base64
import importlib.util
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "scripts" / "provenance_v2.schema.json"
CORE_PATH = REPO_ROOT / "docs" / "sources" / "open-gsd-core-2026-08.bundle.json"
PI_PATH = REPO_ROOT / "docs" / "sources" / "open-gsd-pi-2026-08.bundle.json"


def load_validator():
    path = REPO_ROOT / "scripts" / "validate_skill_sources.py"
    spec = importlib.util.spec_from_file_location("gsd_bundle_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator()


class GsdBundleGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.core = json.loads(CORE_PATH.read_text(encoding="utf-8"))
        cls.pi = json.loads(PI_PATH.read_text(encoding="utf-8"))

    def test_official_bundles_pass_schema_and_behavioral_validation(self):
        Draft202012Validator.check_schema(self.schema)
        schema_validator = Draft202012Validator(
            self.schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        for path, manifest in (
            (CORE_PATH, self.core),
            (PI_PATH, self.pi),
        ):
            with self.subTest(bundle=manifest["bundle"]["id"]):
                self.assertEqual(
                    [],
                    sorted(
                        schema_validator.iter_errors(manifest),
                        key=lambda error: list(error.path),
                    ),
                )
                self.assertEqual(
                    [],
                    validator.validate_mapping(path, REPO_ROOT),
                )

    def test_core_and_pi_commit_relationships_are_explicit_not_forced_equal(self):
        core_installer = self.core["installer"]
        core_slsa = self.core["slsa_provenance"]
        self.assertEqual(
            "ancestor_by",
            self.core["commit_coherence"][
                "attested_source_relative_to_tag"
            ]["relation"],
        )
        self.assertEqual(
            2,
            self.core["commit_coherence"][
                "attested_source_relative_to_tag"
            ]["distance"],
        )
        self.assertNotEqual(
            core_slsa["attested_source_commit"],
            core_installer["tag_commit"],
        )

        pi_installer = self.pi["installer"]
        self.assertEqual(
            "descendant_by",
            self.pi["commit_coherence"][
                "npm_git_head_relative_to_tag"
            ]["relation"],
        )
        self.assertEqual(
            1,
            self.pi["commit_coherence"][
                "npm_git_head_relative_to_tag"
            ]["distance"],
        )
        self.assertNotEqual(
            pi_installer["git_head"],
            pi_installer["tag_commit"],
        )

    def test_relation_gate_rejects_false_equal_and_unsupported_distance(self):
        false_equal = deepcopy(self.pi)
        relation = false_equal["commit_coherence"][
            "npm_git_head_relative_to_tag"
        ]
        relation.update(
            {
                "relation": "equal",
                "distance": 0,
                "ahead_by": 0,
                "base_commit": false_equal["installer"]["tag_commit"],
                "merge_base_commit": false_equal["installer"]["tag_commit"],
            }
        )
        collected = []
        validator._validate_bundle_contract(false_equal, PI_PATH, collected)
        self.assertTrue(
            any("declares equal but the candidate and tag differ" in error for error in collected),
            collected,
        )

        unsupported = deepcopy(self.core)
        unsupported["commit_coherence"][
            "attested_source_relative_to_tag"
        ]["distance"] = 3
        unsupported["commit_coherence"][
            "attested_source_relative_to_tag"
        ]["ahead_by"] = 3
        collected = []
        validator._validate_bundle_contract(unsupported, CORE_PATH, collected)
        self.assertTrue(
            any("distance must be an integer from 0 through 2" in error for error in collected),
            collected,
        )

    def test_supply_chain_subject_and_state_roots_are_isolated(self):
        for manifest in (self.core, self.pi):
            integrity = manifest["installer"]["integrity"].removeprefix(
                "sha512-"
            )
            self.assertEqual(
                base64.b64decode(integrity).hex(),
                manifest["slsa_provenance"]["subject_sha512"],
            )
            self.assertFalse(manifest["install_policy"]["default_install"])
            self.assertEqual(
                "explicit_only",
                manifest["install_policy"]["mode"],
            )
            self.assertFalse(
                manifest["bundle_inventory"]["installed_in_repository"]
            )
            self.assertIsNone(manifest["skills"][0]["repo_skill"])

        self.assertEqual(".planning", self.core["bundle"]["state_root"])
        self.assertEqual(".gsd", self.pi["bundle"]["state_root"])
        self.assertEqual(
            [],
            validator.validate_repository_mappings(
                [CORE_PATH, PI_PATH],
                REPO_ROOT,
            ),
        )

    def test_normal_skill_discovery_cannot_select_pi_bundle(self):
        result = subprocess.run(
            [
                "node",
                str(REPO_ROOT / "bin" / "install-skills.js"),
                "list-skills",
                "--skill",
                "open-gsd-pi-bundle",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("No skills matched", result.stderr)
        self.assertEqual("explicit_only", self.pi["install_policy"]["mode"])
        self.assertTrue(self.pi["bundle"]["optional"])


if __name__ == "__main__":
    unittest.main()

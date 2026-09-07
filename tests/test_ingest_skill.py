import importlib.util
import contextlib
import json
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ingest_skill.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ingest_skill", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mock_license_checkpoint(
    commit: str = "a" * 40,
    *,
    spdx: str = "MIT",
    api_spdx: str | None = "MIT",
) -> dict[str, str]:
    checkpoint = {
        "path": "LICENSE",
        "blob_sha": "e" * 40,
        "content_sha256": "f" * 64,
        "spdx": spdx,
        "resolved_commit": commit,
    }
    if api_spdx is not None:
        checkpoint["api_spdx"] = api_spdx
    return checkpoint


class IngestSkillTests(unittest.TestCase):
    def write_skill(self, root: Path, frontmatter: str) -> Path:
        (root / "docs" / "sources").mkdir(parents=True, exist_ok=True)
        skill_dir = root / "skills" / "developer-engineering" / "sample-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(frontmatter + "\n# Sample\n", encoding="utf-8")
        return skill_dir

    def test_external_ingest_rejects_missing_license(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            skill_dir = self.write_skill(
                Path(tmpdir),
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            with (
                mock.patch.object(
                    module,
                    "resolve_github_checkpoint",
                    return_value=("a" * 40, "b" * 40),
                ),
                mock.patch.object(
                    module,
                    "resolve_commit_bound_github_license",
                    return_value=(
                        None,
                        None,
                        "immutable license evidence is unavailable",
                    ),
                ),
            ):
                ok = module.ingest_one(
                    skill_dir,
                    source="github:owner/repo",
                    source_url="https://github.com/owner/repo",
                    dry_run=True,
                    run_full_validation=False,
                )

            self.assertFalse(ok)

    def test_external_ingest_adds_detected_permissive_license(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            (skill_dir / "SKILL.md").chmod(0o755)
            mapping = root / "docs" / "sources" / "ingested-external.skills.json"
            license_checkpoint = mock_license_checkpoint()
            with (
                mock.patch.object(
                    module,
                    "resolve_commit_bound_github_license",
                    return_value=("MIT", license_checkpoint, None),
                ),
                mock.patch.object(
                    module,
                    "resolve_github_checkpoint",
                    return_value=("a" * 40, "b" * 40),
                ),
                mock.patch.object(
                    module,
                    "classify_github_artifacts",
                    return_value=(
                        [
                            {
                                "source": "skills/sample-skill/SKILL.md",
                                "target": (
                                    "skills/developer-engineering/"
                                    "sample-skill/SKILL.md"
                                ),
                                "type": "file",
                            }
                        ],
                        [],
                    ),
                ),
            ):
                ok = module.ingest_one(
                    skill_dir,
                    source="github:owner/repo",
                    source_url="https://github.com/owner/repo",
                    dry_run=False,
                    external_mapping=mapping,
                    repo_root=root,
                    run_full_validation=False,
                )

            content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(ok)
            self.assertIn("license: MIT", content)
            self.assertIn('source: "github:owner/repo"', content)
            self.assertEqual(
                0o755,
                stat.S_IMODE((skill_dir / "SKILL.md").stat().st_mode),
            )
            payload = json.loads(mapping.read_text(encoding="utf-8"))
            managed = payload["skills"][0]["managed_files"]
            self.assertEqual(
                "100755",
                next(
                    item["mode"]
                    for item in managed
                    if item["path"].endswith("/SKILL.md")
                ),
            )

    def test_dry_run_rejects_dirty_tracked_executable_mode(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            skill_md = skill_dir / "SKILL.md"
            skill_md.chmod(0o644)
            subprocess.run(
                ["git", "-C", str(root), "add", "skills"],
                check=True,
            )
            skill_md.chmod(0o755)

            with self.assertRaisesRegex(
                ValueError,
                "dirty executable-mode changes",
            ):
                module.prepare_ingest(
                    skill_dir,
                    "in-house",
                    "",
                    repo_root=root,
                )

            self.assertEqual(
                0o755,
                stat.S_IMODE(skill_md.stat().st_mode),
            )

    def test_ingest_fingerprint_changes_on_mode_only_mutation(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            mapping = root / "docs" / "sources" / "external.skills.json"
            before = module.capture_ingest_fingerprint(
                skill_dir=skill_dir,
                mapping_path=mapping,
                repo_root=root,
            )
            (skill_dir / "SKILL.md").chmod(0o755)
            after = module.capture_ingest_fingerprint(
                skill_dir=skill_dir,
                mapping_path=mapping,
                repo_root=root,
            )
            self.assertNotEqual(before, after)

    def test_in_house_ingest_does_not_require_license(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            ok = module.ingest_one(
                skill_dir,
                source="in-house",
                source_url="",
                dry_run=True,
                run_full_validation=False,
                repo_root=root,
            )

            self.assertTrue(ok)

    def test_ingest_one_defaults_to_isolated_full_validation(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            before = (skill_dir / "SKILL.md").read_bytes()
            with mock.patch.object(
                module,
                "execute_validated_ingest",
                return_value=True,
            ) as validate:
                self.assertTrue(
                    module.ingest_one(
                        skill_dir,
                        "in-house",
                        "",
                        True,
                        repo_root=root,
                    )
                )
            self.assertTrue(validate.called)
            self.assertTrue(validate.call_args.kwargs["dry_run"])
            self.assertTrue(validate.call_args.kwargs["locks_held"])
            self.assertEqual(before, (skill_dir / "SKILL.md").read_bytes())

    def test_external_ingest_registers_v2_mapping_before_bootstrap(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            (skill_dir / "template.txt").write_text("local supplement\n", encoding="utf-8")
            mapping = root / "docs" / "sources" / "ingested-external.skills.json"
            checkpoint = ("a" * 40, "b" * 40)
            license_checkpoint = mock_license_checkpoint()
            artifacts = (
                [
                    {
                        "source": "skills/sample-skill/SKILL.md",
                        "target": (
                            "skills/developer-engineering/sample-skill/SKILL.md"
                        ),
                        "type": "file",
                    }
                ],
                [
                    {
                        "source": (
                            "skills/developer-engineering/sample-skill/"
                            "template.txt"
                        ),
                        "target": (
                            "skills/developer-engineering/sample-skill/"
                            "template.txt"
                        ),
                        "type": "file",
                    }
                ],
            )
            with (
                mock.patch.object(
                    module,
                    "resolve_commit_bound_github_license",
                    return_value=("MIT", license_checkpoint, None),
                ),
                mock.patch.object(
                    module,
                    "resolve_github_checkpoint",
                    return_value=checkpoint,
                ),
                mock.patch.object(
                    module,
                    "classify_github_artifacts",
                    return_value=artifacts,
                ),
            ):
                ok = module.ingest_one(
                    skill_dir,
                    source="github:owner/repo",
                    source_url=(
                        "https://github.com/owner/repo/tree/main/"
                        "skills/sample-skill"
                    ),
                    dry_run=False,
                    external_mapping=mapping,
                    repo_root=root,
                    run_full_validation=False,
                )

            self.assertTrue(ok)
            payload = json.loads(mapping.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["schema_version"])
            self.assertEqual(1, len(payload["skills"]))
            entry = payload["skills"][0]
            self.assertEqual("overlay", entry["kind"])
            self.assertEqual("monitor", entry["sync_mode"])
            self.assertEqual("owner/repo", entry["origins"][0]["repo"])
            self.assertEqual("local-repo/curation", entry["origins"][1]["repo"])
            self.assertEqual(2, len(entry["managed_files"]))
            self.assertEqual("a" * 40, entry["origins"][0]["tracking"]["resolved_commit"])
            self.assertEqual(
                license_checkpoint,
                entry["origins"][0]["tracking"]["license_checkpoint"],
            )
            self.assertEqual("b" * 40, entry["origins"][0]["tracking"]["path_commit"])
            self.assertEqual(
                "skills/sample-skill/SKILL.md",
                entry["origins"][0]["artifacts"][0]["source"],
            )
            self.assertEqual(
                "skills/developer-engineering/sample-skill/SKILL.md",
                entry["repo_skill"],
            )

            validator_path = REPO_ROOT / "scripts" / "validate_skill_sources.py"
            validator_spec = importlib.util.spec_from_file_location(
                "validate_skill_sources_for_ingest_test",
                validator_path,
            )
            self.assertIsNotNone(validator_spec)
            self.assertIsNotNone(validator_spec.loader)
            validator = importlib.util.module_from_spec(validator_spec)
            validator_spec.loader.exec_module(validator)
            self.assertEqual([], validator.validate_mapping(mapping, root))

            bootstrap_path = REPO_ROOT / "scripts" / "bootstrap_in_house_sources.py"
            spec = importlib.util.spec_from_file_location(
                "bootstrap_in_house_sources_for_ingest_test",
                bootstrap_path,
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            bootstrap = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bootstrap)
            in_house_path = root / "docs" / "sources" / "in-house.skills.json"
            in_house = bootstrap.build_in_house_mapping(
                repo_root=root,
                repo_url="https://github.com/local/repo",
                target_mapping=in_house_path,
                existing_payload=None,
                today="2026-08-20",
            )
            self.assertEqual([], in_house["skills"])

    def test_external_ingest_dry_run_never_creates_mapping(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\nlicense: MIT\n---",
            )
            mapping = root / "docs" / "sources" / "ingested-external.skills.json"
            before = (skill_dir / "SKILL.md").read_bytes()
            license_checkpoint = mock_license_checkpoint()
            with (
                mock.patch.object(
                    module,
                    "resolve_commit_bound_github_license",
                    return_value=("MIT", license_checkpoint, None),
                ),
                mock.patch.object(
                    module,
                    "resolve_github_checkpoint",
                    return_value=("a" * 40, "b" * 40),
                ),
                mock.patch.object(
                    module,
                    "classify_github_artifacts",
                    return_value=(
                        [
                            {
                                "source": "skills/sample-skill/SKILL.md",
                                "target": (
                                    "skills/developer-engineering/"
                                    "sample-skill/SKILL.md"
                                ),
                                "type": "file",
                            }
                        ],
                        [],
                    ),
                ),
            ):
                ok = module.ingest_one(
                    skill_dir,
                    source="github:owner/repo",
                    source_url="https://github.com/owner/repo",
                    dry_run=True,
                    external_mapping=mapping,
                    repo_root=root,
                    run_full_validation=False,
                )
            self.assertTrue(ok)
            self.assertFalse(mapping.exists())
            self.assertEqual(before, (skill_dir / "SKILL.md").read_bytes())

    def test_github_artifact_classification_rejects_unreviewed_body_drift(self):
        module = load_module()

        class FakeProvider:
            def __init__(self, _token):
                pass

            def tree(self, _repo, _commit):
                return {
                    "upstream/SKILL.md": {
                        "type": "blob",
                        "mode": "100644",
                        "sha": "skill-blob",
                    },
                    "upstream/data.bin": {
                        "type": "blob",
                        "mode": "100644",
                        "sha": "data-blob",
                    },
                }

            def blob(self, _repo, blob_sha):
                return {
                    "skill-blob": b"---\nname: sample-skill\n---\n# Upstream\n",
                    "data-blob": b"\x00\x01",
                }[blob_sha]

        fake_module = types.SimpleNamespace(
            GitHubArtifactProvider=FakeProvider,
            GitHubProviderError=RuntimeError,
        )
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            (skill_dir / "data.bin").write_bytes(b"\x00\x01")
            (skill_dir / "local.txt").write_text("local\n", encoding="utf-8")
            with mock.patch.dict(
                sys.modules,
                {"github_artifact_provider": fake_module},
            ):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    module.classify_github_artifacts(
                        skill_dir=skill_dir,
                        repo_root=root,
                        repo="owner/repo",
                        resolved_commit="a" * 40,
                        upstream_skill_path="upstream/SKILL.md",
                    )
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: sample-skill\ndescription: Local\n---\n"
                    "# Upstream\n",
                    encoding="utf-8",
                )
                external, local = module.classify_github_artifacts(
                    skill_dir=skill_dir,
                    repo_root=root,
                    repo="owner/repo",
                    resolved_commit="a" * 40,
                    upstream_skill_path="upstream/SKILL.md",
                    artifact_maps=[
                        (
                            "upstream/data.bin",
                            (
                                "skills/developer-engineering/"
                                "sample-skill/data.bin"
                            ),
                        )
                    ],
                )

            self.assertEqual(
                {"upstream/SKILL.md", "upstream/data.bin"},
                {artifact["source"] for artifact in external},
            )
            self.assertEqual(
                [
                    "skills/developer-engineering/sample-skill/local.txt",
                ],
                [artifact["target"] for artifact in local],
            )

    def test_github_artifact_classification_requires_exact_regular_git_mode(self):
        module = load_module()
        cases = (
            ("skill-644-vs-755", "skill", 0o644, "100755"),
            ("skill-755-vs-644", "skill", 0o755, "100644"),
            ("sidecar-644-vs-755", "sidecar", 0o644, "100755"),
            ("sidecar-755-vs-644", "sidecar", 0o755, "100644"),
            ("skill-symlink-mode", "skill", 0o644, "120000"),
            ("sidecar-symlink-mode", "sidecar", 0o644, "120000"),
        )

        for label, mismatched_target, local_mode, upstream_mode in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
                    root = Path(tmpdir)
                    skill_dir = self.write_skill(
                        root,
                        "---\nname: sample-skill\ndescription: Sample\n---",
                    )
                    skill_path = skill_dir / "SKILL.md"
                    sidecar_path = skill_dir / "data.bin"
                    sidecar_path.write_bytes(b"\x00\x01")
                    skill_path.chmod(
                        local_mode if mismatched_target == "skill" else 0o644
                    )
                    sidecar_path.chmod(
                        local_mode if mismatched_target == "sidecar" else 0o644
                    )
                    skill_bytes = skill_path.read_bytes()
                    tree = {
                        "upstream/SKILL.md": {
                            "type": "blob",
                            "mode": (
                                upstream_mode
                                if mismatched_target == "skill"
                                else "100644"
                            ),
                            "sha": "skill-blob",
                        },
                        "upstream/data.bin": {
                            "type": "blob",
                            "mode": (
                                upstream_mode
                                if mismatched_target == "sidecar"
                                else "100644"
                            ),
                            "sha": "data-blob",
                        },
                    }

                    class FakeProvider:
                        def __init__(self, _token):
                            pass

                        def tree(self, _repo, _commit):
                            return tree

                        def blob(self, _repo, blob_sha):
                            return {
                                "skill-blob": skill_bytes,
                                "data-blob": b"\x00\x01",
                            }[blob_sha]

                    fake_module = types.SimpleNamespace(
                        GitHubArtifactProvider=FakeProvider,
                        GitHubProviderError=RuntimeError,
                    )
                    with mock.patch.dict(
                        sys.modules,
                        {"github_artifact_provider": fake_module},
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "regular Git blob|mode does not match",
                        ):
                            module.classify_github_artifacts(
                                skill_dir=skill_dir,
                                repo_root=root,
                                repo="owner/repo",
                                resolved_commit="a" * 40,
                                upstream_skill_path="upstream/SKILL.md",
                                artifact_maps=[
                                    (
                                        "upstream/data.bin",
                                        (
                                            "skills/developer-engineering/"
                                            "sample-skill/data.bin"
                                        ),
                                    )
                                ],
                            )

    def test_commit_license_overrides_default_branch_and_must_match_frontmatter(self):
        module = load_module()
        evidence = types.SimpleNamespace(
            path="LICENSE",
            blob_sha="e" * 40,
            content_sha256="f" * 64,
            resolved_commit="a" * 40,
            spdx_candidates=("Apache-2.0",),
            api_spdx="NOASSERTION",
        )
        provider = mock.Mock()
        provider.license_evidence.return_value = evidence
        with mock.patch.object(
            module,
            "GitHubArtifactProvider",
            return_value=provider,
        ):
            detected, checkpoint, error = (
                module.resolve_commit_bound_github_license(
                    {"license": "MIT"},
                    "github:owner/repo",
                    "https://github.com/owner/repo",
                    repo="owner/repo",
                    resolved_commit="a" * 40,
                )
            )
        self.assertIsNone(detected)
        self.assertIsNone(checkpoint)
        self.assertIn("does not match", error)
        provider.license_evidence.assert_called_once_with(
            "owner/repo",
            "a" * 40,
        )

    def test_graphify_noassertion_canonical_apache_license_is_accepted(self):
        module = load_module()
        apache = (
            REPO_ROOT
            / "skills"
            / "developer-engineering"
            / "mcp-builder"
            / "LICENSE.txt"
        ).read_bytes()
        evidence = types.SimpleNamespace(
            path="LICENSE",
            blob_sha="e" * 40,
            content_sha256="f" * 64,
            resolved_commit="a" * 40,
            spdx_candidates=(
                module.GitHubArtifactProvider._detect_license_spdx(apache)
            ),
            api_spdx="NOASSERTION",
        )
        provider = mock.Mock()
        provider.license_evidence.return_value = evidence
        with mock.patch.object(
            module,
            "GitHubArtifactProvider",
            return_value=provider,
        ):
            detected, checkpoint, error = (
                module.resolve_commit_bound_github_license(
                    {"license": "Apache-2.0"},
                    "github:Graphify-Labs/graphify",
                    "https://github.com/Graphify-Labs/graphify",
                    repo="Graphify-Labs/graphify",
                    resolved_commit="a" * 40,
                )
            )

        self.assertIsNone(error)
        self.assertEqual("Apache-2.0", detected)
        self.assertEqual("Apache-2.0", checkpoint["spdx"])
        self.assertEqual("NOASSERTION", checkpoint["api_spdx"])
        self.assertEqual("a" * 40, checkpoint["resolved_commit"])

    def test_legacy_external_registration_cannot_bypass_license_check(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                (
                    "---\nname: sample-skill\ndescription: Sample\n"
                    "license: MIT\n---"
                ),
            )
            mapping = (
                root / "docs" / "sources" / "external.skills.json"
            )
            with (
                mock.patch.object(
                    module,
                    "resolve_github_checkpoint",
                    return_value=("a" * 40, "b" * 40),
                ),
                mock.patch.object(
                    module,
                    "resolve_commit_bound_github_license",
                    return_value=(
                        None,
                        None,
                        "detected SPDX 'Apache-2.0' does not match "
                        "origin.license 'MIT'",
                    ),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    module.register_external_provenance(
                        skill_dir=skill_dir,
                        source="github:owner/repo",
                        source_url="https://github.com/owner/repo",
                        license_value="MIT",
                        mapping_path=mapping,
                        repo_root=root,
                    )
            self.assertFalse(mapping.exists())

    def test_strict_github_source_rejects_hostname_and_repo_mismatch(self):
        module = load_module()
        self.assertIsNone(
            module.github_repo_from_source(
                "github:owner/repo",
                "https://github.com.evil.example/owner/repo",
            )
        )
        detected, error = module.resolve_external_license(
            {"license": "MIT"},
            "github:owner/repo",
            "https://github.com/other/repo",
        )
        self.assertIsNone(detected)
        self.assertIn("disagree", error)

    def test_supplied_checkpoint_mismatch_fails_before_any_write(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            skill_path = skill_dir / "SKILL.md"
            before = skill_path.read_bytes()
            mapping = root / "docs" / "sources" / "external.skills.json"
            with (
                mock.patch.object(
                    module,
                    "resolve_github_checkpoint",
                    return_value=("a" * 40, "b" * 40),
                ),
            ):
                ok = module.ingest_one(
                    skill_dir,
                    "github:owner/repo",
                    "https://github.com/owner/repo",
                    False,
                    repo_root=root,
                    external_mapping=mapping,
                    resolved_commit="c" * 40,
                    run_full_validation=False,
                )
            self.assertFalse(ok)
            self.assertEqual(before, skill_path.read_bytes())
            self.assertFalse(mapping.exists())

    def test_non_github_license_string_without_evidence_is_rejected(self):
        module = load_module()
        detected, error = module.resolve_external_license(
            {"license": "MIT"},
            "skills.sh",
            "https://skills.sh/example/skill",
        )
        self.assertIsNone(detected)
        self.assertIn("cannot prove immutable content lineage", error)

    def test_batch_claim_inventory_scans_all_v2_mappings(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            sources = Path(tmpdir)
            for index, name in enumerate(("alpha", "beta")):
                (sources / f"{index}.skills.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "skills": [
                                {
                                    "normalized_slug": name,
                                    "status": "verified_in_repo",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            self.assertEqual(
                {"alpha", "beta"},
                module.get_tracked_skills(sources),
            )

    def test_artifact_manifest_requires_explicit_cross_directory_targets(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            manifest = Path(tmpdir) / "artifacts.json"
            manifest.write_text(
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "source": "packages/docs/reference.md",
                                "target": (
                                    "skills/developer-engineering/"
                                    "sample-skill/references/reference.md"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    (
                        "packages/docs/reference.md",
                        (
                            "skills/developer-engineering/"
                            "sample-skill/references/reference.md"
                        ),
                    )
                ],
                module.load_artifact_manifest(manifest),
            )

    def test_atomic_ingest_restores_frontmatter_when_mapping_replace_fails(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            skill_path = skill_dir / "SKILL.md"
            mapping = root / "docs" / "sources" / "external.skills.json"
            plan = module.IngestPlan(
                skill_name="sample-skill",
                skill_md=skill_path,
                before_skill=skill_path.read_bytes(),
                after_skill=skill_path.read_bytes() + b"\nsource: test\n",
                mapping_path=mapping,
                before_mapping=None,
                after_mapping=b"{}\n",
            )
            original_write = module._write_atomic_bytes
            calls = 0

            def flaky_write(path, content, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("mapping replace failed")
                return original_write(path, content, **kwargs)

            with mock.patch.object(
                module, "_write_atomic_bytes", side_effect=flaky_write
            ):
                with self.assertRaisesRegex(OSError, "mapping replace failed"):
                    module.commit_ingest_plans([plan])
            self.assertEqual(plan.before_skill, skill_path.read_bytes())
            self.assertFalse(mapping.exists())
            self.assertTrue((root / "docs" / "sources").is_dir())

    def test_post_replace_directory_fsync_failure_restores_current_file(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_path = root / "SKILL.md"
            skill_path.write_bytes(b"before")
            plan = module.IngestPlan(
                skill_name="demo",
                skill_md=skill_path,
                before_skill=b"before",
                after_skill=b"after",
                repo_root=root,
                before_checkpoint=module.capture_target_checkpoint(
                    skill_path,
                    repo_root=root,
                ),
            )
            real_fsync = module.os.fsync
            calls = 0

            def fail_first_directory_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")
                return real_fsync(descriptor)

            with mock.patch.object(
                module.os,
                "fsync",
                side_effect=fail_first_directory_sync,
            ):
                with self.assertRaisesRegex(
                    OSError, "directory fsync failed"
                ):
                    module.commit_ingest_plans(
                        [plan],
                        locks_held=True,
                    )
            self.assertEqual(b"before", skill_path.read_bytes())

    def test_sidecar_or_claim_race_rejects_stale_ingest_plan(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            skill_dir = self.write_skill(
                root,
                "---\nname: sample-skill\ndescription: Sample\n---",
            )
            sidecar = skill_dir / "template.txt"
            sidecar.write_text("before", encoding="utf-8")
            plan = module.prepare_ingest(
                skill_dir,
                "in-house",
                "",
                repo_root=root,
            )
            sidecar.write_text("after", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "race detected"):
                module.commit_ingest_plans([plan])
            self.assertEqual(
                plan.before_skill,
                (skill_dir / "SKILL.md").read_bytes(),
            )

    def test_post_ingest_pipeline_is_fail_fast(self):
        module = load_module()
        failed = types.SimpleNamespace(
            returncode=7,
            stdout="gate failed",
            stderr="details",
        )
        with mock.patch.object(
            module.subprocess,
            "run",
            return_value=failed,
        ) as run:
            self.assertFalse(module.run_pipeline(False))
        self.assertEqual(1, run.call_count)
        inventory_command = [
            module.sys.executable,
            "scripts/reconcile_artifact_inventory.py",
            "--offline",
            "--check-clean",
            "--quiet",
        ]
        self.assertIn(inventory_command, module.pipeline_commands())

    def test_dry_run_validates_staged_diff_without_repository_writes(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            (root / "docs" / "sources").mkdir(parents=True)
            target = root / "SKILL.md"
            target.write_bytes(b"before")
            mutation = module.IngestPlan(
                skill_name="staged:SKILL.md",
                skill_md=target,
                before_skill=b"before",
                after_skill=b"after",
            )
            baseline = module.repository_bytes_snapshot(root)
            baseline_checkpoints = module.repository_checkpoint_snapshot(
                root,
                set(baseline),
            )
            tracked_reports = module.tracked_report_paths(root)
            with mock.patch.object(
                module,
                "validate_ingest_plans",
                return_value=(
                    [mutation],
                    baseline,
                    baseline_checkpoints,
                    tracked_reports,
                ),
            ):
                self.assertTrue(
                    module.execute_validated_ingest(
                        [mutation],
                        repo_root=root,
                        dry_run=True,
                        locks_held=True,
                    )
                )
            self.assertEqual(b"before", target.read_bytes())

    def test_staged_mutation_keeps_original_target_checkpoint(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            (root / "docs" / "sources").mkdir(parents=True)
            skill_dir = root / "skills" / "category" / "demo"
            skill_dir.mkdir(parents=True)
            target = skill_dir / "SKILL.md"
            target.write_bytes(b"before")
            checkpoint = module.capture_target_checkpoint(
                target,
                repo_root=root,
            )
            plan = module.IngestPlan(
                skill_name="demo",
                skill_md=target,
                before_skill=b"before",
                after_skill=b"after",
                repo_root=root,
                before_checkpoint=checkpoint,
            )

            with mock.patch.object(module, "run_pipeline", return_value=True):
                validated = module.validate_ingest_plans(
                    [plan],
                    repo_root=root,
                )

            self.assertIsNotNone(validated)
            mutations, _baseline, _checkpoints, _tracked_reports = validated
            mutation = next(
                item for item in mutations if item.skill_md == target
            )
            self.assertEqual(checkpoint, mutation.before_checkpoint)
            self.assertEqual(
                {"exists", "dev", "ino", "mode", "size", "sha256"},
                set(mutation.before_checkpoint),
            )
            self.assertEqual(b"before", target.read_bytes())

    def test_staged_mode_only_mutation_is_materialized_and_applied(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            (root / "docs" / "sources").mkdir(parents=True)
            skill_dir = root / "skills" / "category" / "demo"
            skill_dir.mkdir(parents=True)
            target = skill_dir / "SKILL.md"
            target.write_bytes(b"same bytes")
            target.chmod(0o644)
            checkpoint = module.capture_target_checkpoint(
                target,
                repo_root=root,
            )
            plan = module.IngestPlan(
                skill_name="demo",
                skill_md=target,
                before_skill=b"same bytes",
                after_skill=b"same bytes",
                repo_root=root,
                before_checkpoint=checkpoint,
            )

            def chmod_in_stage(_dry_run, *, repo_root):
                staged_target = (
                    repo_root / "skills" / "category" / "demo" / "SKILL.md"
                )
                staged_target.chmod(0o755)
                return True

            with mock.patch.object(
                module,
                "run_pipeline",
                side_effect=chmod_in_stage,
            ):
                validated = module.validate_ingest_plans(
                    [plan],
                    repo_root=root,
                )

            self.assertIsNotNone(validated)
            mutations, _baseline, _checkpoints, _tracked_reports = validated
            mutation = next(
                item for item in mutations if item.skill_md == target
            )
            self.assertEqual(b"same bytes", mutation.before_skill)
            self.assertEqual(b"same bytes", mutation.after_skill)
            self.assertEqual(0o755, mutation.after_mode)
            self.assertEqual(0o644, stat.S_IMODE(target.stat().st_mode))

            module.commit_ingest_plans(mutations, locks_held=True)
            self.assertEqual(b"same bytes", target.read_bytes())
            self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))

    def test_failed_mode_only_batch_restores_original_mode(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"same")
            second.write_bytes(b"before")
            first.chmod(0o644)
            plans = [
                module.IngestPlan(
                    skill_name="first",
                    skill_md=first,
                    before_skill=b"same",
                    after_skill=b"same",
                    after_mode=0o755,
                    repo_root=root,
                    before_checkpoint=module.capture_target_checkpoint(
                        first,
                        repo_root=root,
                    ),
                ),
                module.IngestPlan(
                    skill_name="second",
                    skill_md=second,
                    before_skill=b"before",
                    after_skill=b"after",
                    repo_root=root,
                    before_checkpoint=module.capture_target_checkpoint(
                        second,
                        repo_root=root,
                    ),
                ),
            ]
            real_write = module._write_atomic_bytes
            calls = 0

            def fail_second(path, content, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second target failed")
                return real_write(path, content, **kwargs)

            with mock.patch.object(
                module,
                "_write_atomic_bytes",
                side_effect=fail_second,
            ):
                with self.assertRaisesRegex(OSError, "second target failed"):
                    module.commit_ingest_plans(plans, locks_held=True)

            self.assertEqual(b"same", first.read_bytes())
            self.assertEqual(0o644, stat.S_IMODE(first.stat().st_mode))
            self.assertEqual(b"before", second.read_bytes())

    def test_noop_validation_ignores_generated_reports_but_copies_them(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            reports = root / "docs" / "sources" / "reports"
            reports.mkdir(parents=True)
            generated_json = reports / "skill-quality-audit.json"
            generated_markdown = reports / "skill-quality-audit.md"
            historic = reports / "skill-curation-2026-04-25.md"
            generated_json.write_text('{"before":true}\n', encoding="utf-8")
            generated_markdown.write_text("# Before\n", encoding="utf-8")
            historic.write_text("# Historic\n", encoding="utf-8")
            skill_dir = root / "skills" / "category" / "demo"
            skill_dir.mkdir(parents=True)
            target = skill_dir / "SKILL.md"
            target.write_bytes(b"before")
            tracked = {
                "docs/sources/reports/skill-curation-2026-04-25.md"
            }
            plan = module.IngestPlan(
                skill_name="demo",
                skill_md=target,
                before_skill=b"before",
                after_skill=b"before",
                repo_root=root,
                before_checkpoint=module.capture_target_checkpoint(
                    target,
                    repo_root=root,
                ),
                before_parent_checkpoint=module.capture_parent_checkpoint(
                    root,
                    target,
                ),
            )

            def mutate_generated_reports(_dry_run, *, repo_root):
                staged_reports = repo_root / "docs" / "sources" / "reports"
                self.assertEqual(
                    '{"before":true}\n',
                    (staged_reports / generated_json.name).read_text(),
                )
                self.assertEqual(
                    "# Historic\n",
                    (staged_reports / historic.name).read_text(),
                )
                (staged_reports / generated_json.name).write_text(
                    '{"after":true}\n',
                    encoding="utf-8",
                )
                (staged_reports / generated_markdown.name).write_text(
                    "# After\n",
                    encoding="utf-8",
                )
                return True

            with (
                mock.patch.object(
                    module,
                    "tracked_report_paths",
                    return_value=tracked,
                ),
                mock.patch.object(
                    module,
                    "run_pipeline",
                    side_effect=mutate_generated_reports,
                ),
            ):
                validated = module.validate_ingest_plans(
                    [plan],
                    repo_root=root,
                )

            self.assertIsNotNone(validated)
            mutations, baseline, _checkpoints, reported_tracked = validated
            self.assertEqual([], mutations)
            self.assertEqual(tracked, reported_tracked)
            self.assertIn(
                "docs/sources/reports/skill-curation-2026-04-25.md",
                baseline,
            )
            self.assertNotIn(
                "docs/sources/reports/skill-quality-audit.json",
                baseline,
            )
            self.assertNotIn(
                "docs/sources/reports/skill-quality-audit.md",
                baseline,
            )
            self.assertEqual('{"before":true}\n', generated_json.read_text())
            self.assertEqual("# Before\n", generated_markdown.read_text())

    def test_stage_copy_rejects_symlink_without_touching_external_sentinel(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            base = Path(tmpdir)
            root = base / "repo"
            root.mkdir()
            (root / "docs" / "sources").mkdir(parents=True)
            skill_dir = root / "skills" / "category" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\n---\n# Demo\n",
                encoding="utf-8",
            )
            sentinel = base / "outside.txt"
            sentinel.write_text("do-not-touch", encoding="utf-8")
            (skill_dir / "escape").symlink_to(sentinel)
            destination = base / "stage"

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                module._copy_stage_repository(root, destination)

            self.assertEqual("do-not-touch", sentinel.read_text())
            self.assertFalse(destination.exists())

    def test_stage_copy_exports_trusted_index_modes_without_git_directory(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            base = Path(tmpdir)
            root = base / "repo"
            root.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
            )
            canonical = root / "skills" / "category" / "demo" / "SKILL.md"
            exported = root / "openclaw-skills" / "demo" / "SKILL.md"
            canonical.parent.mkdir(parents=True)
            exported.parent.mkdir(parents=True)
            canonical.write_text("---\nname: demo\n---\n", encoding="utf-8")
            exported.write_bytes(canonical.read_bytes())
            canonical.chmod(0o755)
            exported.chmod(0o755)
            subprocess.run(
                ["git", "-C", str(root), "add", "skills", "openclaw-skills"],
                check=True,
            )

            destination = base / "stage"
            module._copy_stage_repository(root, destination)

            self.assertFalse((destination / ".git").exists())
            snapshot_path = (
                destination / module.STAGE_INDEX_MODES_NAME
            )
            self.assertEqual(0o600, stat.S_IMODE(snapshot_path.stat().st_mode))
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual("trusted-git-index", snapshot["source"])
            self.assertEqual(
                "100755",
                snapshot["modes"]["skills/category/demo/SKILL.md"],
            )
            self.assertEqual(
                "100755",
                snapshot["modes"]["openclaw-skills/demo/SKILL.md"],
            )

    def test_repo_root_ancestor_symlink_is_rejected_nofollow(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            base = Path(tmpdir)
            real_root = base / "real"
            real_root.mkdir()
            alias = base / "alias"
            alias.symlink_to(real_root, target_is_directory=True)
            with self.assertRaises(OSError):
                module.open_directory_nofollow(alias)

    def test_ingest_and_sync_share_mapping_and_skill_lock_identities(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            sources = root / "docs" / "sources"
            sources.mkdir(parents=True)
            mapping = sources / "source.skills.json"
            mapping.write_text(
                '{"schema_version":2,"skills":[]}\n',
                encoding="utf-8",
            )
            skill_dir = root / "skills" / "category" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\n---\n# Demo\n",
                encoding="utf-8",
            )

            with module.acquire_ingest_locks(
                repo_root=root,
                skill_dirs=[skill_dir],
                mapping_paths=[mapping],
                timeout=0,
            ):
                with self.assertRaisesRegex(Exception, "already active"):
                    with module.mapping_advisory_lock(mapping, timeout=0):
                        pass
                with self.assertRaisesRegex(
                    Exception, "already active"
                ):
                    with module.skill_advisory_lock(
                        root,
                        "skills/category/demo",
                        timeout=0,
                    ):
                        pass

            with module.mapping_advisory_lock(mapping, timeout=0):
                with self.assertRaisesRegex(Exception, "already active"):
                    with module.acquire_ingest_locks(
                        repo_root=root,
                        skill_dirs=[skill_dir],
                        mapping_paths=[mapping],
                        timeout=0,
                    ):
                        pass

    def test_ingest_skill_lock_allows_engine_recovery_before_revalidation(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            sources = root / "docs" / "sources"
            sources.mkdir(parents=True)
            mapping = sources / "source.skills.json"
            mapping.write_text(
                '{"schema_version":2,"skills":[]}\n',
                encoding="utf-8",
            )
            skill_dir = root / "skills" / "category" / "demo"
            skill_dir.parent.mkdir(parents=True)
            entered = []

            @contextlib.contextmanager
            def recovering_skill_lock(
                repo_root,
                skill_root,
                *,
                timeout,
                recover_pending,
            ):
                entered.append(
                    (
                        Path(repo_root),
                        skill_root,
                        timeout,
                        recover_pending,
                    )
                )
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: demo\n---\n# Demo\n",
                    encoding="utf-8",
                )
                yield

            with mock.patch.object(
                module,
                "skill_advisory_lock",
                side_effect=recovering_skill_lock,
            ):
                with module.acquire_ingest_locks(
                    repo_root=root,
                    skill_dirs=[skill_dir],
                    mapping_paths=[mapping],
                    timeout=0,
                ):
                    self.assertTrue(skill_dir.is_dir())

            self.assertEqual(
                [(root, "skills/category/demo", 0, True)],
                entered,
            )

    def test_batch_checkpoint_race_is_rejected_before_first_write(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"first-before")
            second.write_bytes(b"second-before")
            plans = []
            for path, after in (
                (first, b"first-after"),
                (second, b"second-after"),
            ):
                plans.append(
                    module.IngestPlan(
                        skill_name=path.name,
                        skill_md=path,
                        before_skill=path.read_bytes(),
                        after_skill=after,
                        repo_root=root,
                        before_checkpoint=module.capture_target_checkpoint(
                            path,
                            repo_root=root,
                        ),
                    )
                )
            replacement = root / "replacement"
            replacement.write_bytes(b"second-before")
            replacement.replace(second)

            with self.assertRaisesRegex(
                RuntimeError, "changed after staging"
            ):
                module.commit_ingest_plans(plans, locks_held=True)

            self.assertEqual(b"first-before", first.read_bytes())
            self.assertEqual(b"second-before", second.read_bytes())

    def test_mode_race_is_rejected_before_first_write(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            target = root / "target.txt"
            target.write_bytes(b"before")
            target.chmod(0o644)
            plan = module.IngestPlan(
                skill_name="target",
                skill_md=target,
                before_skill=b"before",
                after_skill=b"after",
                repo_root=root,
                before_checkpoint=module.capture_target_checkpoint(
                    target,
                    repo_root=root,
                ),
            )
            target.chmod(0o755)

            with self.assertRaisesRegex(
                RuntimeError,
                "changed after staging",
            ):
                module.commit_ingest_plans([plan], locks_held=True)

            self.assertEqual(b"before", target.read_bytes())
            self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))

    def test_atomic_commit_securely_creates_missing_target_parents(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            target = root / "generated" / "nested" / "artifact.txt"
            plan = module.IngestPlan(
                skill_name="generated",
                skill_md=target,
                before_skill=None,
                after_skill=b"generated",
                repo_root=root,
                before_checkpoint=module.capture_target_checkpoint(
                    target,
                    repo_root=root,
                ),
                before_parent_checkpoint=module.capture_parent_checkpoint(
                    root,
                    target,
                ),
            )

            module.commit_ingest_plans([plan], locks_held=True)

            self.assertEqual(b"generated", target.read_bytes())
            self.assertFalse(target.is_symlink())

    def test_failed_batch_removes_new_target_and_created_parents(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            generated = root / "generated" / "nested" / "artifact.txt"
            existing = root / "existing.txt"
            existing.write_bytes(b"before")
            plans = [
                module.IngestPlan(
                    skill_name="generated",
                    skill_md=generated,
                    before_skill=None,
                    after_skill=b"generated",
                    repo_root=root,
                    before_checkpoint=module.capture_target_checkpoint(
                        generated,
                        repo_root=root,
                    ),
                    before_parent_checkpoint=(
                        module.capture_parent_checkpoint(root, generated)
                    ),
                ),
                module.IngestPlan(
                    skill_name="existing",
                    skill_md=existing,
                    before_skill=b"before",
                    after_skill=b"after",
                    repo_root=root,
                    before_checkpoint=module.capture_target_checkpoint(
                        existing,
                        repo_root=root,
                    ),
                    before_parent_checkpoint=(
                        module.capture_parent_checkpoint(root, existing)
                    ),
                ),
            ]
            real_write = module._write_atomic_bytes
            calls = 0

            def fail_second(path, content, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second target failed")
                return real_write(path, content, **kwargs)

            with mock.patch.object(
                module,
                "_write_atomic_bytes",
                side_effect=fail_second,
            ):
                with self.assertRaisesRegex(OSError, "second target failed"):
                    module.commit_ingest_plans(
                        plans,
                        locks_held=True,
                    )

            self.assertFalse(generated.exists())
            self.assertFalse((root / "generated").exists())
            self.assertEqual(b"before", existing.read_bytes())

    def test_secure_writer_rechecks_destination_inode_before_replace(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            root = Path(tmpdir)
            target = root / "target.txt"
            target.write_bytes(b"before")
            checkpoint = module.capture_target_checkpoint(
                target,
                repo_root=root,
            )
            real_capture = module._capture_target_at
            target_captures = 0

            def replace_at_boundary(parent_fd, leaf, path):
                nonlocal target_captures
                if path == target:
                    target_captures += 1
                    if target_captures == 3:
                        replacement = root / "replacement.txt"
                        replacement.write_bytes(b"before")
                        replacement.replace(target)
                return real_capture(parent_fd, leaf, path)

            with mock.patch.object(
                module,
                "_capture_target_at",
                side_effect=replace_at_boundary,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "changed immediately before replace",
                ):
                    module._write_atomic_bytes_secure(
                        target,
                        b"after",
                        repo_root=root,
                        expected_checkpoint=checkpoint,
                    )

            self.assertEqual(b"before", target.read_bytes())
            self.assertEqual([], list(root.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()

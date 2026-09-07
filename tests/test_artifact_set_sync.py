from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import artifact_set_sync as artifact_sync
from scripts.artifact_set_sync import (
    ArtifactApplyError,
    ArtifactLockError,
    ArtifactRecoveryError,
    ArtifactValidationError,
    ConcurrentModificationError,
    OwnershipConflictError,
    apply_artifact_set_sync,
    plan_artifact_set_sync,
    prepare_artifact_set_sync,
    skill_advisory_lock,
    skill_lock_identity,
    skill_lock_path,
    skill_transaction_journal_path,
    sync_artifact_set,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(root: Path, relative: str, data: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _entry(
    files: dict[str, bytes],
    *,
    slug: str = "demo",
    repo_skill: str = "skills/ai-workflow/demo/SKILL.md",
) -> dict:
    skill_root = Path(repo_skill).parent.as_posix()
    return {
        "normalized_slug": slug,
        "repo_skill": repo_skill,
        "kind": "mirror",
        "origins": [
            {
                "artifacts": [
                    {
                        "source": f"upstream/{slug}",
                        "target": skill_root,
                        "type": "directory",
                    }
                ]
            }
        ],
        "managed_files": [
            {
                "path": path,
                "sha256": _digest(data),
                "owner": slug,
                "mode": "100644",
            }
            for path, data in sorted(files.items())
        ],
    }


def _payload(
    source: str,
    target: str,
    data: bytes,
    kind: str = "file",
    mode: str = "100644",
) -> dict:
    return {
        "source": source,
        "target": target,
        "type": kind,
        "data": data,
        "mode": mode,
    }


def _temp_artifacts(root: Path) -> list[Path]:
    parent = root / "skills/ai-workflow"
    return (
        sorted(parent.glob(".demo.artifact-*")) if parent.exists() else []
    )


def _run_hard_exit_worker(
    root: Path,
    *,
    mode: str,
    mapping: Path | None = None,
    before_sha256: str | None = None,
    after_sha256: str | None = None,
) -> subprocess.CompletedProcess[str]:
    script = r"""
import hashlib
import os
import sys
from pathlib import Path
from scripts import artifact_set_sync as artifact_sync
from scripts.artifact_set_sync import plan_artifact_set_sync, prepare_artifact_set_sync

root = Path(sys.argv[1])
mode = sys.argv[2]
skill = "skills/ai-workflow/demo/SKILL.md"
old = (root / skill).read_bytes()
entry = {
    "normalized_slug": "demo",
    "repo_skill": skill,
    "kind": "mirror",
    "origins": [{
        "artifacts": [{
            "source": "release/demo",
            "target": "skills/ai-workflow/demo",
            "type": "directory",
        }],
    }],
    "managed_files": [{
        "path": skill,
        "sha256": hashlib.sha256(old).hexdigest(),
        "owner": "demo",
        "mode": "100644",
    }],
}
plan = plan_artifact_set_sync(
    root,
    entry,
    [{"source": "release/SKILL.md", "target": skill, "type": "file", "data": b"new body", "mode": "100644"}],
    {"ref": "v2"},
)

real_replace_tree = artifact_sync._replace_tree_and_fsync
def replace_tree_and_hard_exit(source, destination):
    real_replace_tree(source, destination)
    if mode == "after-backup-before-state" and destination.name == "previous":
        os._exit(74)
    if (
        mode == "after-install-before-state"
        and destination == root / "skills/ai-workflow/demo"
    ):
        os._exit(75)

artifact_sync._replace_tree_and_fsync = replace_tree_and_hard_exit

def fault(event):
    if mode == "after-backup" and event == "after_backup_rename":
        os._exit(71)

transaction = prepare_artifact_set_sync(plan, fault_injector=fault)
if mode == "prepared-unbound":
    os._exit(72)
if mode in {"bound-before", "bound-after"}:
    mapping = Path(sys.argv[3])
    before_sha256 = sys.argv[4]
    after_sha256 = sys.argv[5]
    transaction.bind_authority(mapping, before_sha256, after_sha256)
    if mode == "bound-before":
        os._exit(76)
    replacement = mapping.with_suffix(".next")
    replacement.write_bytes(b'{"state":"after"}\n')
    with replacement.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(replacement, mapping)
    directory_fd = os.open(mapping.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os._exit(73)
raise RuntimeError(mode)
"""
    command = [sys.executable, "-c", script, str(root), mode]
    if mapping is not None:
        command.extend(
            [
                str(mapping),
                str(before_sha256),
                str(after_sha256),
            ]
        )
    env = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = (
        str(repo_root)
        + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    return subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_successful_multisidecar_sync_returns_json_ready_metadata(tmp_path: Path):
    old = {
        "skills/ai-workflow/demo/SKILL.md": b"old body\n",
        "skills/ai-workflow/demo/references/guide.md": b"old guide\n",
    }
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    checkpoint = {
        "channel": "latest_release",
        "ref": "v2.0.0",
        "resolved_commit": "a" * 40,
        "content_sha256": "0" * 64,
    }
    artifacts = [
        _payload(
            "package/skills/demo/references/guide.md",
            "skills/ai-workflow/demo/references/guide.md",
            b"new guide\n",
        ),
        _payload(
            "package/skills/demo/templates/request.md",
            "skills/ai-workflow/demo/templates/request.md",
            b"template\n",
        ),
        _payload(
            "package/skills/demo/scripts/check.sh",
            "skills/ai-workflow/demo/scripts/check.sh",
            b"#!/bin/sh\nexit 0\n",
            mode="100755",
        ),
        _payload(
            "package/skills/demo/SKILL.md",
            "skills/ai-workflow/demo/SKILL.md",
            b"new body\n",
        ),
    ]

    result = sync_artifact_set(tmp_path, entry, artifacts, checkpoint)

    assert result.applied
    assert not result.dry_run
    assert result.changed == (
        "skills/ai-workflow/demo/SKILL.md",
        "skills/ai-workflow/demo/references/guide.md",
        "skills/ai-workflow/demo/scripts/check.sh",
        "skills/ai-workflow/demo/templates/request.md",
    )
    assert result.pruned == ()
    assert (tmp_path / "skills/ai-workflow/demo/SKILL.md").read_bytes() == b"new body\n"
    assert (
        tmp_path / "skills/ai-workflow/demo/references/guide.md"
    ).read_bytes() == b"new guide\n"
    assert result.content_sha256 == _digest(b"new body\n")
    assert result.checkpoint["content_sha256"] == _digest(b"new body\n")
    assert checkpoint["content_sha256"] == "0" * 64
    patch = result.metadata_patch()
    assert [item["target"] for item in patch["artifacts"]] == [
        item["path"] for item in patch["managed_files"]
    ]
    assert all(item["owner"] == "demo" for item in patch["managed_files"])
    script = tmp_path / "skills/ai-workflow/demo/scripts/check.sh"
    assert script.stat().st_mode & 0o777 == 0o755
    assert next(
        item
        for item in patch["managed_files"]
        if item["path"].endswith("/scripts/check.sh")
    )["mode"] == "100755"
    assert _temp_artifacts(tmp_path) == []


@pytest.mark.parametrize(
    ("old_mode", "new_mode"),
    [("100644", "100755"), ("100755", "100644")],
)
def test_mode_only_change_is_planned_and_applied(
    tmp_path: Path,
    old_mode: str,
    new_mode: str,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    body = b"same body\n"
    _write(tmp_path, skill, body)
    path = tmp_path / skill
    path.chmod(0o755 if old_mode == "100755" else 0o644)
    entry = _entry({skill: body})
    entry["managed_files"][0]["mode"] = old_mode

    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [_payload("release/SKILL.md", skill, body, mode=new_mode)],
        {"ref": "v2"},
    )

    assert plan.changed == (skill,)
    result = apply_artifact_set_sync(plan)
    assert result.applied
    assert path.read_bytes() == body
    assert path.stat().st_mode & 0o777 == (
        0o755 if new_mode == "100755" else 0o644
    )
    assert result.managed_files[0]["mode"] == new_mode


@pytest.mark.parametrize("mode", [None, "100600", 0o755])
def test_managed_manifest_requires_canonical_git_file_mode(
    tmp_path: Path,
    mode,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    body = b"body"
    _write(tmp_path, skill, body)
    entry = _entry({skill: body})
    if mode is None:
        entry["managed_files"][0].pop("mode")
    else:
        entry["managed_files"][0]["mode"] = mode

    with pytest.raises(ArtifactValidationError, match=r"\.mode"):
        plan_artifact_set_sync(
            tmp_path,
            entry,
            [_payload("release/SKILL.md", skill, body)],
            {"ref": "v2"},
        )


def test_binary_bytes_and_cross_directory_source_mapping_are_exact(tmp_path: Path):
    old = {"skills/ai-workflow/demo/SKILL.md": b"body"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    binary = b"\x00\xff\x10PNG\r\n\x1a\n"
    artifacts = [
        _payload(
            "codex/SKILL.md",
            "skills/ai-workflow/demo/SKILL.md",
            b"new body",
        ),
        {
            "source": "shared-assets/icons/logo.bin",
            "target": "skills/ai-workflow/demo/assets/logo.bin",
            "type": "binary",
            "bytes": binary,
            "mode": "100644",
        },
    ]

    result = sync_artifact_set(tmp_path, entry, artifacts, {"ref": "v1"})

    assert (
        tmp_path / "skills/ai-workflow/demo/assets/logo.bin"
    ).read_bytes() == binary
    logo = next(
        item
        for item in result.managed_files
        if item["path"].endswith("logo.bin")
    )
    assert logo["sha256"] == _digest(binary)
    assert all(item["type"] == "file" for item in result.artifacts)


def test_source_and_target_rename_prunes_only_old_managed_file(tmp_path: Path):
    old = {
        "skills/ai-workflow/demo/SKILL.md": b"body",
        "skills/ai-workflow/demo/references/old.md": b"owned old",
    }
    for path, data in old.items():
        _write(tmp_path, path, data)
    _write(
        tmp_path,
        "skills/ai-workflow/demo/notes/user.md",
        b"keep me",
    )
    entry = _entry(old)
    artifacts = [
        _payload(
            "relocated/codex-skill.md",
            "skills/ai-workflow/demo/SKILL.md",
            b"body",
        ),
        _payload(
            "relocated/docs/current.md",
            "skills/ai-workflow/demo/references/current.md",
            b"owned old",
        ),
    ]

    result = sync_artifact_set(tmp_path, entry, artifacts, {"ref": "v2"})

    assert result.changed == (
        "skills/ai-workflow/demo/references/current.md",
    )
    assert result.pruned == (
        "skills/ai-workflow/demo/references/old.md",
    )
    assert not (
        tmp_path / "skills/ai-workflow/demo/references/old.md"
    ).exists()
    assert (
        tmp_path / "skills/ai-workflow/demo/references/current.md"
    ).read_bytes() == b"owned old"
    assert (
        tmp_path / "skills/ai-workflow/demo/notes/user.md"
    ).read_bytes() == b"keep me"
    skill_artifact = next(
        item for item in result.artifacts if item["target"].endswith("SKILL.md")
    )
    assert skill_artifact["source"] == "relocated/codex-skill.md"


def test_unowned_files_are_preserved_and_not_added_to_new_manifest(tmp_path: Path):
    old = {"skills/ai-workflow/demo/SKILL.md": b"old"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    unowned = "skills/ai-workflow/demo/local/user-notes.txt"
    _write(tmp_path, unowned, b"user content")
    entry = _entry(old)

    result = sync_artifact_set(
        tmp_path,
        entry,
        [
            _payload(
                "upstream/SKILL.md",
                "skills/ai-workflow/demo/SKILL.md",
                b"new",
            )
        ],
        {"ref": "v2"},
    )

    assert unowned in result.preserved
    assert (tmp_path / unowned).read_bytes() == b"user content"
    assert unowned not in {item["path"] for item in result.managed_files}


def test_unowned_target_collision_is_never_silently_adopted(tmp_path: Path):
    old = {"skills/ai-workflow/demo/SKILL.md": b"body"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    target = "skills/ai-workflow/demo/references/existing.md"
    _write(tmp_path, target, b"same bytes")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [
            _payload(
                "upstream/SKILL.md",
                "skills/ai-workflow/demo/SKILL.md",
                b"body",
            ),
            _payload("upstream/existing.md", target, b"same bytes"),
        ],
        {"ref": "v1"},
    )

    assert plan.unowned_conflicts == (target,)
    with pytest.raises(OwnershipConflictError) as raised:
        apply_artifact_set_sync(plan)
    assert raised.value.unowned_conflicts == (target,)
    assert (tmp_path / target).read_bytes() == b"same bytes"


@pytest.mark.parametrize("entry_shape", ["multi_origin", "overlay"])
def test_implicit_ownership_is_only_allowed_for_single_origin_mirror(
    tmp_path: Path, entry_shape: str
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"body"}
    _write(tmp_path, skill, b"body")
    entry = _entry(old)
    if entry_shape == "multi_origin":
        entry["origins"].append({"artifacts": []})
    else:
        entry["kind"] = "overlay"

    with pytest.raises(ArtifactValidationError, match="implicit ownership"):
        plan_artifact_set_sync(
            tmp_path,
            entry,
            [_payload("release/SKILL.md", skill, b"new")],
            {"ref": "v2"},
        )


def test_origin_scope_prunes_selected_artifacts_and_protects_overlay_sidecars(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    external = "skills/ai-workflow/demo/references/external-old.md"
    local = "skills/ai-workflow/demo/references/local-curation.md"
    old = {
        skill: b"old body",
        external: b"external",
        local: b"local supplement",
    }
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    entry["origins"] = [
        {
            "artifacts": [
                {"source": "upstream/SKILL.md", "target": skill, "type": "file"},
                {
                    "source": "upstream/external-old.md",
                    "target": external,
                    "type": "file",
                },
            ]
        },
        {
            "artifacts": [
                {"source": local, "target": local, "type": "file"},
            ]
        },
    ]

    result = sync_artifact_set(
        tmp_path,
        entry,
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
        origin_index=0,
    )

    assert result.pruned == (external,)
    assert not (tmp_path / external).exists()
    assert (tmp_path / local).read_bytes() == b"local supplement"
    returned = {item["path"]: item["sha256"] for item in result.managed_files}
    assert returned == {
        skill: _digest(b"new body"),
        local: _digest(b"local supplement"),
    }


def test_origin_scope_rejects_payload_collision_with_another_origin(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    local = "skills/ai-workflow/demo/references/local-curation.md"
    old = {skill: b"body", local: b"curated"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    entry["origins"] = [
        {
            "artifacts": [
                {"source": "upstream/SKILL.md", "target": skill, "type": "file"},
                {
                    "source": "upstream/references",
                    "target": "skills/ai-workflow/demo/references",
                    "type": "directory",
                },
            ]
        },
        {
            "artifacts": [
                {"source": local, "target": local, "type": "file"},
            ]
        },
    ]
    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [
            _payload("release/SKILL.md", skill, b"new"),
            _payload("release/local-curation.md", local, b"overwrite"),
        ],
        {"ref": "v2"},
        origin_index=0,
    )

    assert plan.protected_targets == (local,)
    assert plan.unowned_conflicts == (local,)
    with pytest.raises(OwnershipConflictError):
        apply_artifact_set_sync(plan)
    assert (tmp_path / local).read_bytes() == b"curated"


def test_origin_directory_claim_expands_only_across_managed_inventory(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    external = "skills/ai-workflow/demo/references/external.md"
    local = "skills/ai-workflow/demo/local/curation.md"
    old = {skill: b"body", external: b"external", local: b"local"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    entry["origins"] = [
        {
            "artifacts": [
                {
                    "source": "release/demo/SKILL.md",
                    "target": skill,
                    "type": "file",
                },
                {
                    "source": "release/demo/references",
                    "target": "skills/ai-workflow/demo/references",
                    "type": "directory",
                }
            ]
        }
    ]

    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [_payload("release/demo/SKILL.md", skill, b"new")],
        {"ref": "v2"},
        origin_index=0,
    )

    assert plan.owned_targets == (skill, external)
    assert plan.protected_targets == (local,)
    result = apply_artifact_set_sync(plan)
    assert result.pruned == (external,)
    assert (tmp_path / local).read_bytes() == b"local"


def test_other_origin_exact_file_is_protected_from_selected_root_directory(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    external = "skills/ai-workflow/demo/references/external.md"
    local = "skills/ai-workflow/demo/local/curation.md"
    old = {skill: b"body", external: b"external", local: b"local"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    entry["origins"] = [
        {
            "artifacts": [
                {
                    "source": "release/demo",
                    "target": "skills/ai-workflow/demo",
                    "type": "directory",
                }
            ]
        },
        {
            "artifacts": [
                {"source": local, "target": local, "type": "file"},
            ]
        },
    ]

    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [_payload("release/demo/SKILL.md", skill, b"new")],
        {"ref": "v2"},
        origin_index=0,
    )

    assert plan.owned_targets == (skill, external)
    assert plan.protected_targets == (local,)
    result = apply_artifact_set_sync(plan)
    assert result.pruned == (external,)
    assert (tmp_path / local).read_bytes() == b"local"
    assert local in {item["path"] for item in result.managed_files}


def test_other_origin_directory_scope_protects_new_selected_payload(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"body"}
    _write(tmp_path, skill, b"body")
    entry = _entry(old)
    entry["origins"] = [
        {
            "artifacts": [
                {"source": "release/SKILL.md", "target": skill, "type": "file"},
                {
                    "source": "release/references",
                    "target": "skills/ai-workflow/demo/references",
                    "type": "directory",
                },
            ]
        },
        {
            "artifacts": [
                {
                    "source": "curation/references",
                    "target": "skills/ai-workflow/demo/references",
                    "type": "directory",
                }
            ]
        },
    ]
    target = "skills/ai-workflow/demo/references/new.md"

    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [
            _payload("release/SKILL.md", skill, b"new"),
            _payload("release/references/new.md", target, b"new reference"),
        ],
        {"ref": "v2"},
        origin_index=0,
    )

    assert target in plan.protected_targets
    assert target in plan.unowned_conflicts
    with pytest.raises(OwnershipConflictError):
        apply_artifact_set_sync(plan)


def test_selected_origin_must_claim_every_payload_target(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    _write(tmp_path, skill, b"body")
    entry = _entry({skill: b"body"})
    entry["origins"][0]["artifacts"] = [
        {"source": "release/SKILL.md", "target": skill, "type": "file"}
    ]
    target = "skills/ai-workflow/demo/references/unclaimed.md"

    with pytest.raises(ArtifactValidationError, match="selected origin"):
        plan_artifact_set_sync(
            tmp_path,
            entry,
            [
                _payload("release/SKILL.md", skill, b"new"),
                _payload("release/unclaimed.md", target, b"unclaimed"),
            ],
            {"ref": "v2"},
            origin_index=0,
        )


@pytest.mark.parametrize("remove", [False, True])
def test_modified_managed_overwrite_or_delete_is_blocked(
    tmp_path: Path, remove: bool
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    sidecar = "skills/ai-workflow/demo/references/owned.md"
    old = {skill: b"body", sidecar: b"manifest version"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    _write(tmp_path, sidecar, b"user modification")
    artifacts = [_payload("upstream/SKILL.md", skill, b"body")]
    if not remove:
        artifacts.append(
            _payload("upstream/owned.md", sidecar, b"upstream replacement")
        )

    plan = plan_artifact_set_sync(tmp_path, entry, artifacts, {"ref": "v2"})

    assert plan.user_modified == (sidecar,)
    assert any(
        item.path == sidecar and item.kind == "hash_mismatch"
        for item in plan.drift
    )
    with pytest.raises(OwnershipConflictError) as raised:
        apply_artifact_set_sync(plan, dry_run=True)
    assert raised.value.user_modified == (sidecar,)
    assert (tmp_path / sidecar).read_bytes() == b"user modification"
    assert _temp_artifacts(tmp_path) == []


def test_modified_managed_file_equal_to_new_payload_is_safe_drift(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    sidecar = "skills/ai-workflow/demo/references/owned.md"
    old = {skill: b"body", sidecar: b"manifest version"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    entry = _entry(old)
    _write(tmp_path, sidecar, b"already upstream")
    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [
            _payload("upstream/SKILL.md", skill, b"body"),
            _payload("upstream/owned.md", sidecar, b"already upstream"),
        ],
        {"ref": "v2"},
    )

    assert not plan.blocked
    assert plan.changed == ()
    assert len(plan.drift) == 1
    result = apply_artifact_set_sync(plan)
    assert not result.applied
    assert result.managed_files[1]["sha256"] == _digest(b"already upstream")


def test_missing_managed_file_is_reported_as_drift_and_recreated(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    sidecar = "skills/ai-workflow/demo/references/missing.md"
    old = {skill: b"body", sidecar: b"old"}
    _write(tmp_path, skill, b"body")
    entry = _entry(old)
    plan = plan_artifact_set_sync(
        tmp_path,
        entry,
        [
            _payload("upstream/SKILL.md", skill, b"body"),
            _payload("upstream/missing.md", sidecar, b"restored"),
        ],
        {"ref": "v2"},
    )

    assert plan.user_modified == ()
    assert plan.drift[0].to_dict() == {
        "path": sidecar,
        "kind": "missing",
        "expected_sha256": _digest(b"old"),
        "actual_sha256": None,
    }
    apply_artifact_set_sync(plan)
    assert (tmp_path / sidecar).read_bytes() == b"restored"


def test_dry_run_performs_strictly_zero_writes(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old"}
    _write(tmp_path, skill, b"old")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = sync_artifact_set(
        tmp_path,
        _entry(old),
        [_payload("upstream/SKILL.md", skill, b"new")],
        {"ref": "v2"},
        dry_run=True,
    )

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result.dry_run and not result.applied
    assert before == after
    assert _temp_artifacts(tmp_path) == []


def test_symlink_parent_escape_is_rejected_without_touching_outside(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"body"}
    _write(tmp_path, skill, b"body")
    outside = tmp_path / "outside"
    outside.mkdir()
    references = tmp_path / "skills/ai-workflow/demo/references"
    references.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactValidationError, match="symlink"):
        plan_artifact_set_sync(
            tmp_path,
            _entry(old),
            [
                _payload("upstream/SKILL.md", skill, b"new body"),
                _payload(
                    "upstream/escape.md",
                    "skills/ai-workflow/demo/references/escape.md",
                    b"escape",
                ),
            ],
            {"ref": "v2"},
        )

    assert list(outside.iterdir()) == []
    assert (tmp_path / skill).read_bytes() == b"body"


def test_symlink_introduced_during_stage_copy_is_rejected_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    reference = "skills/ai-workflow/demo/references/old.md"
    old = {skill: b"body", reference: b"old"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    outside = tmp_path / "outside"
    outside.mkdir()
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [
            _payload("upstream/SKILL.md", skill, b"new body"),
            _payload(
                "upstream/new.md",
                "skills/ai-workflow/demo/references/new.md",
                b"must not escape",
            ),
        ],
        {"ref": "v2"},
    )
    real_copytree = artifact_sync.shutil.copytree

    def hostile_copytree(source, destination, *args, **kwargs):
        result = real_copytree(source, destination, *args, **kwargs)
        if Path(destination).name == "new":
            references = Path(destination) / "references"
            artifact_sync.shutil.rmtree(references)
            references.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(artifact_sync.shutil, "copytree", hostile_copytree)
    with pytest.raises(ArtifactValidationError, match="symlink"):
        apply_artifact_set_sync(plan)

    assert list(outside.iterdir()) == []
    assert (tmp_path / skill).read_bytes() == b"body"
    assert (tmp_path / reference).read_bytes() == b"old"
    assert _temp_artifacts(tmp_path) == []


def test_directory_payload_must_be_expanded_and_paths_must_be_canonical(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    _write(tmp_path, skill, b"body")
    entry = _entry({skill: b"body"})
    with pytest.raises(ArtifactValidationError, match="expanded"):
        plan_artifact_set_sync(
            tmp_path,
            entry,
            [_payload("upstream/demo", skill, b"", "directory")],
            {},
        )
    with pytest.raises(ArtifactValidationError, match="canonical"):
        plan_artifact_set_sync(
            tmp_path,
            entry,
            [_payload("upstream/SKILL.md", "skills/ai-workflow/demo/../x", b"x")],
            {},
        )


@pytest.mark.parametrize(
    "event", ["after_backup_rename", "after_install_rename"]
)
def test_fault_after_rename_rolls_back_and_removes_all_transaction_dirs(
    tmp_path: Path, event: str
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    sidecar = "skills/ai-workflow/demo/references/old.md"
    old = {skill: b"old body", sidecar: b"old ref"}
    for path, data in old.items():
        _write(tmp_path, path, data)
    _write(
        tmp_path,
        "skills/ai-workflow/demo/local.txt",
        b"unowned",
    )
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [
            _payload(
                "upstream/SKILL.md",
                skill,
                b"new body",
                mode="100755",
            ),
            _payload(
                "upstream/new.md",
                "skills/ai-workflow/demo/references/new.md",
                b"new ref",
            ),
        ],
        {"ref": "v2"},
    )

    def fail(current: str) -> None:
        if current == event:
            raise RuntimeError(f"injected at {event}")

    with pytest.raises(ArtifactApplyError) as raised:
        apply_artifact_set_sync(plan, fault_injector=fail)

    assert raised.value.rollback_succeeded
    assert (tmp_path / skill).read_bytes() == b"old body"
    assert (tmp_path / skill).stat().st_mode & 0o777 == 0o644
    assert (tmp_path / sidecar).read_bytes() == b"old ref"
    assert (
        tmp_path / "skills/ai-workflow/demo/local.txt"
    ).read_bytes() == b"unowned"
    assert not (
        tmp_path / "skills/ai-workflow/demo/references/new.md"
    ).exists()
    assert _temp_artifacts(tmp_path) == []


def test_mapping_finalize_failure_rolls_back_prepared_artifact_set(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "mapping.json"
    mapping.write_bytes(b'{"checkpoint":"old"}\n')
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    with (
        pytest.raises(OSError, match="mapping replace failed"),
        prepare_artifact_set_sync(plan) as transaction,
    ):
        assert (tmp_path / skill).read_bytes() == b"new body"
        assert transaction.state == "prepared"
        # The integration's atomic mapping writer fails before replace.
        raise OSError("mapping replace failed")

    assert (tmp_path / skill).read_bytes() == b"old body"
    assert mapping.read_bytes() == b'{"checkpoint":"old"}\n'
    assert _temp_artifacts(tmp_path) == []


def test_concurrent_tree_swap_in_final_scan_rename_window_is_not_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = tmp_path / "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry({skill: b"old body"}),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )
    real_replace = artifact_sync.os.replace
    swapped = False

    def hostile_replace(source, destination):
        nonlocal swapped
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not swapped
            and source_path == skill_root
            and destination_path.name == "previous"
        ):
            swapped = True
            displaced = skill_root.parent / "displaced-by-concurrent-writer"
            real_replace(skill_root, displaced)
            skill_root.mkdir()
            (skill_root / "SKILL.md").write_bytes(b"concurrent body")
            (skill_root / "concurrent.txt").write_bytes(b"must survive")
        return real_replace(source, destination)

    monkeypatch.setattr(artifact_sync.os, "replace", hostile_replace)
    with pytest.raises(ConcurrentModificationError):
        apply_artifact_set_sync(plan)

    survivors = [
        path
        for path in tmp_path.rglob("concurrent.txt")
        if path.read_bytes() == b"must survive"
    ]
    assert survivors
    assert (skill_root / "concurrent.txt").read_bytes() == b"must survive"


def test_mapping_failure_after_replace_never_leaves_skill_directory_missing(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "mapping.json"
    mapping.write_bytes(b'{"checkpoint":"old"}\n')
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    with (
        pytest.raises(OSError, match="post-replace failure"),
        prepare_artifact_set_sync(plan),
    ):
        replacement = tmp_path / "mapping.json.next"
        replacement.write_bytes(b'{"checkpoint":"new"}\n')
        replacement.replace(mapping)
        raise OSError("post-replace failure")

    # The mapping writer owns restoration of its own post-replace failure, but
    # the artifact transaction independently guarantees a live old directory.
    assert mapping.read_bytes() == b'{"checkpoint":"new"}\n'
    assert (tmp_path / skill).read_bytes() == b"old body"
    assert (tmp_path / "skills/ai-workflow/demo").is_dir()
    assert _temp_artifacts(tmp_path) == []


def test_prepared_transaction_commits_only_after_mapping_finalize(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    with prepare_artifact_set_sync(plan) as transaction:
        assert (tmp_path / skill).read_bytes() == b"new body"
        result = transaction.commit()

    assert result.applied
    assert transaction.state == "committed"
    assert (tmp_path / skill).read_bytes() == b"new body"
    assert _temp_artifacts(tmp_path) == []


def test_bound_authority_requires_mapping_after_hash_before_commit(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "docs/sources/example.skills.json"
    mapping.parent.mkdir(parents=True)
    before = b'{"state":"before"}\n'
    after = b'{"state":"after"}\n'
    mapping.write_bytes(before)
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    with prepare_artifact_set_sync(plan) as transaction:
        transaction.bind_authority(mapping, _digest(before), _digest(after))
        with pytest.raises(ArtifactRecoveryError, match="before.*after hash"):
            transaction.commit()
        replacement = mapping.with_suffix(".next")
        replacement.write_bytes(after)
        replacement.replace(mapping)
        transaction.commit()

    assert mapping.read_bytes() == after
    assert (tmp_path / skill).read_bytes() == b"new body"
    assert not skill_transaction_journal_path(tmp_path, skill_root).exists()


def test_bind_authority_rejects_symlink_and_non_before_digest(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    outside = tmp_path / "outside-mapping.json"
    outside.write_bytes(b"outside")
    mapping = tmp_path / "docs/sources/example.skills.json"
    mapping.parent.mkdir(parents=True)
    mapping.symlink_to(outside)
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    with (
        pytest.raises(ArtifactRecoveryError, match="unsafe path"),
        prepare_artifact_set_sync(plan) as transaction,
    ):
        transaction.bind_authority(
            mapping,
            _digest(b"outside"),
            _digest(b"after"),
        )

    mapping.unlink()
    mapping.write_bytes(b"before")
    next_plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )
    with (
        pytest.raises(ConcurrentModificationError),
        prepare_artifact_set_sync(next_plan) as transaction,
    ):
        transaction.bind_authority(
            mapping,
            _digest(b"different"),
            _digest(b"after"),
        )

    assert (tmp_path / skill).read_bytes() == b"old body"


def test_bound_after_mapping_is_kept_when_context_exits_with_error(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "docs/sources/example.skills.json"
    mapping.parent.mkdir(parents=True)
    before = b"before\n"
    after = b"after\n"
    mapping.write_bytes(before)
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry({skill: b"old body"}),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    with pytest.raises(OSError, match="post-replace fsync"):
        with prepare_artifact_set_sync(plan) as transaction:
            transaction.bind_authority(
                mapping,
                _digest(before),
                _digest(after),
            )
            replacement = mapping.with_suffix(".next")
            replacement.write_bytes(after)
            replacement.replace(mapping)
            raise OSError("post-replace fsync")

    assert mapping.read_bytes() == after
    assert (tmp_path / skill).read_bytes() == b"new body"


def test_per_skill_lock_covers_prepare_through_finalize_and_rollback(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old"}
    _write(tmp_path, skill, b"old")
    entry = _entry(old)
    artifacts = [_payload("release/SKILL.md", skill, b"new")]
    first_plan = plan_artifact_set_sync(
        tmp_path, entry, artifacts, {"ref": "v2"}
    )
    first = prepare_artifact_set_sync(first_plan)
    try:
        with pytest.raises(ArtifactLockError):
            prepare_artifact_set_sync(first_plan, lock_timeout=0.01)
        with pytest.raises(ArtifactLockError):
            apply_artifact_set_sync(first_plan, lock_timeout=0.01)
    finally:
        first.rollback()

    second_plan = plan_artifact_set_sync(
        tmp_path, entry, artifacts, {"ref": "v2"}
    )
    second = prepare_artifact_set_sync(second_plan)
    second.rollback()
    assert (tmp_path / skill).read_bytes() == b"old"
    assert _temp_artifacts(tmp_path) == []


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True])
def test_invalid_lock_timeout_is_rejected_without_artifact_writes(
    tmp_path: Path, timeout
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old"}
    _write(tmp_path, skill, b"old")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new")],
        {"ref": "v2"},
    )

    with pytest.raises(ArtifactValidationError, match="lock_timeout"):
        prepare_artifact_set_sync(plan, lock_timeout=timeout)

    assert (tmp_path / skill).read_bytes() == b"old"
    assert _temp_artifacts(tmp_path) == []


def test_advisory_lock_is_released_when_lock_holder_process_crashes(
    tmp_path: Path,
):
    if artifact_sync.fcntl is None:
        pytest.skip("POSIX advisory locks are unavailable")
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old"}
    _write(tmp_path, skill, b"old")
    entry = _entry(old)
    artifacts = [_payload("release/SKILL.md", skill, b"new")]
    plan = plan_artifact_set_sync(
        tmp_path, entry, artifacts, {"ref": "v2"}
    )
    initial = prepare_artifact_set_sync(plan)
    lock_path = initial.lock_path
    initial.rollback()

    script = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(ArtifactLockError):
            prepare_artifact_set_sync(plan, lock_timeout=0.01)
        process.kill()
        process.wait(timeout=5)

        recovered_plan = plan_artifact_set_sync(
            tmp_path, entry, artifacts, {"ref": "v2"}
        )
        recovered = prepare_artifact_set_sync(recovered_plan)
        recovered.rollback()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert (tmp_path / skill).read_bytes() == b"old"


def test_public_skill_lock_api_reuses_engine_identity(tmp_path: Path):
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, f"{skill_root}/SKILL.md", b"body")

    identity = skill_lock_identity(tmp_path, skill_root)
    lock_path = skill_lock_path(tmp_path, skill_root)

    assert lock_path.stem == identity
    with skill_advisory_lock(tmp_path, skill_root, timeout=0.0) as lock:
        assert lock.path == lock_path
        with pytest.raises(ArtifactLockError):
            with skill_advisory_lock(tmp_path, skill_root, timeout=0.01):
                pass


@pytest.mark.parametrize(
    ("mode", "exit_code"),
    [
        ("after-backup", 71),
        ("prepared-unbound", 72),
        ("after-backup-before-state", 74),
        ("after-install-before-state", 75),
    ],
)
def test_hard_exit_without_mapping_authority_restores_old_tree(
    tmp_path: Path,
    mode: str,
    exit_code: int,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")

    completed = _run_hard_exit_worker(tmp_path, mode=mode)

    assert completed.returncode == exit_code, completed.stderr
    journal = skill_transaction_journal_path(tmp_path, skill_root)
    assert journal.is_file() and not journal.is_symlink()
    with skill_advisory_lock(tmp_path, skill_root):
        pass

    assert (tmp_path / skill).read_bytes() == b"old body"
    assert not journal.exists()
    assert _temp_artifacts(tmp_path) == []


def test_hard_exit_after_binding_before_mapping_restores_old_tree(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "docs/sources/example.skills.json"
    mapping.parent.mkdir(parents=True)
    before = b'{"state":"before"}\n'
    after = b'{"state":"after"}\n'
    mapping.write_bytes(before)

    completed = _run_hard_exit_worker(
        tmp_path,
        mode="bound-before",
        mapping=mapping,
        before_sha256=_digest(before),
        after_sha256=_digest(after),
    )

    assert completed.returncode == 76, completed.stderr
    journal = skill_transaction_journal_path(tmp_path, skill_root)
    assert journal.exists()
    with skill_advisory_lock(tmp_path, skill_root):
        pass

    assert mapping.read_bytes() == before
    assert (tmp_path / skill).read_bytes() == b"old body"
    assert not journal.exists()
    assert _temp_artifacts(tmp_path) == []


def test_hard_exit_after_bound_mapping_commit_keeps_new_tree(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "docs/sources/example.skills.json"
    mapping.parent.mkdir(parents=True)
    before = b'{"state":"before"}\n'
    after = b'{"state":"after"}\n'
    mapping.write_bytes(before)

    completed = _run_hard_exit_worker(
        tmp_path,
        mode="bound-after",
        mapping=mapping,
        before_sha256=_digest(before),
        after_sha256=_digest(after),
    )

    assert completed.returncode == 73, completed.stderr
    journal = skill_transaction_journal_path(tmp_path, skill_root)
    assert journal.exists()
    with skill_advisory_lock(tmp_path, skill_root):
        pass

    assert mapping.read_bytes() == after
    assert (tmp_path / skill).read_bytes() == b"new body"
    assert not journal.exists()
    assert _temp_artifacts(tmp_path) == []


def test_ambiguous_bound_mapping_preserves_recovery_and_fails(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    mapping = tmp_path / "docs/sources/example.skills.json"
    mapping.parent.mkdir(parents=True)
    before = b'{"state":"before"}\n'
    after = b'{"state":"after"}\n'
    mapping.write_bytes(before)
    completed = _run_hard_exit_worker(
        tmp_path,
        mode="bound-after",
        mapping=mapping,
        before_sha256=_digest(before),
        after_sha256=_digest(after),
    )
    assert completed.returncode == 73, completed.stderr
    mapping.write_bytes(b'{"state":"ambiguous"}\n')
    journal = skill_transaction_journal_path(tmp_path, skill_root)

    with pytest.raises(ArtifactRecoveryError) as raised:
        with skill_advisory_lock(tmp_path, skill_root):
            pass

    assert journal in raised.value.recovery_paths
    assert journal.exists()
    assert (tmp_path / skill).read_bytes() == b"new body"

    mapping.write_bytes(before)
    with skill_advisory_lock(tmp_path, skill_root):
        pass
    assert (tmp_path / skill).read_bytes() == b"old body"
    assert not journal.exists()


def test_tampered_journal_path_and_symlink_are_fail_closed(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    completed = _run_hard_exit_worker(tmp_path, mode="prepared-unbound")
    assert completed.returncode == 72, completed.stderr
    journal = skill_transaction_journal_path(tmp_path, skill_root)
    original = journal.read_bytes()
    payload = json.loads(original)
    payload["state"] = "rolling_back"
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactRecoveryError, match="integrity hash mismatch"):
        with skill_advisory_lock(tmp_path, skill_root):
            pass
    assert (tmp_path / skill).read_bytes() == b"new body"

    payload = json.loads(original)
    payload["stage_container"] = "../escape"
    payload["journal_sha256"] = artifact_sync._computed_journal_integrity(
        payload
    )
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactRecoveryError, match="canonical relative POSIX"):
        with skill_advisory_lock(tmp_path, skill_root):
            pass
    assert (tmp_path / skill).read_bytes() == b"new body"

    journal.write_bytes(original)
    outside = tmp_path / "outside-journal.json"
    outside.write_bytes(original)
    journal.unlink()
    journal.symlink_to(outside)
    with pytest.raises(ArtifactRecoveryError, match="regular non-symlink"):
        with skill_advisory_lock(tmp_path, skill_root):
            pass

    journal.unlink()
    journal.write_bytes(original)
    with skill_advisory_lock(tmp_path, skill_root):
        pass
    assert (tmp_path / skill).read_bytes() == b"old body"
    assert not journal.exists()


def test_byte_only_v1_journal_is_rejected_for_manual_recovery(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    completed = _run_hard_exit_worker(tmp_path, mode="prepared-unbound")
    assert completed.returncode == 72, completed.stderr
    journal = skill_transaction_journal_path(tmp_path, skill_root)
    original = journal.read_bytes()
    payload = json.loads(original)
    assert payload["version"] == 2
    payload["version"] = 1
    payload["journal_sha256"] = artifact_sync._computed_journal_integrity(
        payload
    )
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ArtifactRecoveryError,
        match="unsupported transaction journal version",
    ) as raised:
        with skill_advisory_lock(tmp_path, skill_root):
            pass

    assert journal in raised.value.recovery_paths
    assert journal.exists()
    journal.write_bytes(original)
    with skill_advisory_lock(tmp_path, skill_root):
        pass
    assert (tmp_path / skill).read_bytes() == b"old body"
    assert not journal.exists()


def test_active_transaction_journal_tamper_restores_old_and_releases_lock(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry({skill: b"old body"}),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    transaction = prepare_artifact_set_sync(plan)
    journal = skill_transaction_journal_path(tmp_path, skill_root)
    original = journal.read_bytes()
    payload = json.loads(original)
    payload["state"] = "rolling_back"
    journal.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactApplyError, match="failed to roll back"):
        transaction.rollback()

    assert (tmp_path / skill).read_bytes() == b"old body"
    assert journal.exists()
    journal.write_bytes(original)
    with skill_advisory_lock(tmp_path, skill_root):
        pass
    assert not journal.exists()


def test_plan_created_before_recovery_requires_retry(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    completed = _run_hard_exit_worker(tmp_path, mode="prepared-unbound")
    assert completed.returncode == 72, completed.stderr
    assert (tmp_path / skill).read_bytes() == b"new body"

    stale_plan = plan_artifact_set_sync(
        tmp_path,
        _entry({skill: b"new body"}),
        [_payload("release/SKILL.md", skill, b"third body")],
        {"ref": "v3"},
    )
    with pytest.raises(ConcurrentModificationError):
        prepare_artifact_set_sync(stale_plan)

    assert (tmp_path / skill).read_bytes() == b"old body"
    assert not skill_transaction_journal_path(tmp_path, skill_root).exists()
    retry_plan = plan_artifact_set_sync(
        tmp_path,
        _entry({skill: b"old body"}),
        [_payload("release/SKILL.md", skill, b"third body")],
        {"ref": "v3"},
    )
    retried = prepare_artifact_set_sync(retry_plan)
    retried.rollback()


def test_journal_is_stable_before_first_rename_and_tree_parents_are_fsynced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = "skills/ai-workflow/demo"
    _write(tmp_path, skill, b"old body")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry({skill: b"old body"}),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )
    fsynced: list[Path] = []
    real_fsync_directory = artifact_sync._fsync_directory

    def record_fsync(path: Path) -> None:
        fsynced.append(Path(path))
        real_fsync_directory(path)

    monkeypatch.setattr(artifact_sync, "_fsync_directory", record_fsync)
    observed_state = None

    def inspect_journal(event: str) -> None:
        nonlocal observed_state
        if event == "after_stage_built":
            journal = skill_transaction_journal_path(tmp_path, skill_root)
            payload = json.loads(journal.read_text(encoding="utf-8"))
            observed_state = payload["state"]
            assert payload["journal_sha256"]
            assert journal.stat().st_mode & 0o777 == 0o600
            if hasattr(os, "getuid"):
                assert journal.stat().st_uid == os.getuid()

    transaction = prepare_artifact_set_sync(plan, fault_injector=inspect_journal)
    transaction.rollback()

    skill_parent = tmp_path / "skills/ai-workflow"
    assert observed_state == "staged"
    assert fsynced.count(skill_parent) >= 3
    assert skill_lock_path(tmp_path, skill_root).parent in fsynced


def test_concurrent_rollback_occupant_is_quarantined_before_old_restore(
    tmp_path: Path,
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_root = tmp_path / "skills/ai-workflow/demo"
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )

    def race(event: str) -> None:
        if event == "after_backup_rename":
            skill_root.mkdir()
            (skill_root / "concurrent.txt").write_bytes(b"do not delete")
            raise RuntimeError("force rollback after concurrent create")

    with pytest.raises(ArtifactApplyError) as raised:
        prepare_artifact_set_sync(plan, fault_injector=race)

    error = raised.value
    assert error.rollback_succeeded
    assert error.recovery_path is not None
    assert (tmp_path / skill).read_bytes() == b"old body"
    occupants = list(error.recovery_path.glob("concurrent-occupant-*"))
    assert len(occupants) == 1
    assert (occupants[0] / "concurrent.txt").read_bytes() == b"do not delete"
    assert not list(
        (tmp_path / "skills/ai-workflow").glob(".demo.artifact-stage-*")
    )
    artifact_sync.shutil.rmtree(error.recovery_path)


def test_failed_old_restore_keeps_unique_backup_and_reports_recovery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    skill = "skills/ai-workflow/demo/SKILL.md"
    skill_path = tmp_path / skill
    old = {skill: b"old body"}
    _write(tmp_path, skill, b"old body")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("release/SKILL.md", skill, b"new body")],
        {"ref": "v2"},
    )
    real_replace = artifact_sync.os.replace

    def fail_restore(source, destination):
        if (
            Path(source).name == "previous"
            and Path(destination) == skill_path.parent
        ):
            raise OSError("injected restore failure")
        return real_replace(source, destination)

    monkeypatch.setattr(artifact_sync.os, "replace", fail_restore)

    def fail_after_backup(event: str) -> None:
        if event == "after_backup_rename":
            raise RuntimeError("force rollback")

    with pytest.raises(ArtifactApplyError) as raised:
        prepare_artifact_set_sync(plan, fault_injector=fail_after_backup)

    error = raised.value
    assert not error.rollback_succeeded
    assert error.recovery_path is not None
    backup = error.recovery_path / "previous" / "SKILL.md"
    assert backup.read_bytes() == b"old body"
    assert not skill_path.parent.exists()
    # Cleanup is explicit only after the preserved backup has been observed.
    artifact_sync.shutil.rmtree(error.recovery_path)
    skill_transaction_journal_path(
        tmp_path,
        "skills/ai-workflow/demo",
    ).unlink(missing_ok=True)


def test_concurrent_change_after_plan_is_rejected_before_staging(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    unowned = "skills/ai-workflow/demo/local.txt"
    old = {skill: b"old"}
    _write(tmp_path, skill, b"old")
    _write(tmp_path, unowned, b"one")
    plan = plan_artifact_set_sync(
        tmp_path,
        _entry(old),
        [_payload("upstream/SKILL.md", skill, b"new")],
        {"ref": "v2"},
    )
    _write(tmp_path, unowned, b"two")

    with pytest.raises(ConcurrentModificationError) as raised:
        apply_artifact_set_sync(plan)

    assert raised.value.paths == (unowned,)
    assert (tmp_path / skill).read_bytes() == b"old"
    assert (tmp_path / unowned).read_bytes() == b"two"
    assert _temp_artifacts(tmp_path) == []


def test_second_run_with_returned_manifest_is_idempotent(tmp_path: Path):
    skill = "skills/ai-workflow/demo/SKILL.md"
    old = {skill: b"old"}
    _write(tmp_path, skill, b"old")
    artifacts = [
        _payload("upstream/SKILL.md", skill, b"new"),
        _payload(
            "upstream/reference.md",
            "skills/ai-workflow/demo/references/reference.md",
            b"reference",
        ),
    ]
    first = sync_artifact_set(
        tmp_path, _entry(old), artifacts, {"ref": "v2"}
    )
    next_entry = _entry({}, slug="demo")
    next_entry["managed_files"] = [
        dict(item) for item in first.managed_files
    ]
    before = {
        path.relative_to(tmp_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    second = sync_artifact_set(
        tmp_path, next_entry, artifacts, dict(first.checkpoint)
    )

    after = {
        path.relative_to(tmp_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert not second.applied
    assert second.changed == ()
    assert second.pruned == ()
    assert second.drift == ()
    assert before == after
    assert _temp_artifacts(tmp_path) == []

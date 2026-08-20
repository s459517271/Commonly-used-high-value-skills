from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.durable_file_batch import (
    DurableBatchError,
    DurableBatchRecoveryError,
    durable_batch_lock_and_recover,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def run_crashing_child(code: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, str(root), str(SCRIPTS)],
        capture_output=True,
        text=True,
        check=False,
    )


def tree_digest(root: Path) -> str:
    digest = __import__("hashlib").sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.S_IFMT(metadata.st_mode)).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
        digest.update(b"\0")
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class DurableFileBatchTests(unittest.TestCase):
    def test_public_guard_preserves_explicit_after_mode(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            target = root / "mapping.json"
            target.write_bytes(b"before")
            target.chmod(0o600)

            with durable_batch_lock_and_recover(root) as guard:
                def apply() -> None:
                    target.write_bytes(b"after")
                    target.chmod(0o600)

                guard.commit_batch(
                    {target: b"after"},
                    apply,
                    after_modes={target: 0o600},
                )

            self.assertEqual(b"after", target.read_bytes())
            self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
            self.assertFalse((root / ".hvs-transactions/pending").exists())

    def test_sync_batch_hard_exit_recovers_on_next_entry(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            first = root / "first.skills.json"
            second = root / "second.skills.json"
            first.write_text('{"value":"before-one"}\n', encoding="utf-8")
            second.write_text('{"value":"before-two"}\n', encoding="utf-8")
            before = {first: first.read_bytes(), second: second.read_bytes()}
            code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import sync_upstream as sync
root = Path(sys.argv[1])
sync.REPO_ROOT = root
first = root / "first.skills.json"
second = root / "second.skills.json"
def crash(event, path):
    if event == "after_replace" and path == first:
        os._exit(73)
sync.atomic_write_json_batch(
    {first: {"value": "after-one"}, second: {"value": "after-two"}},
    fault_injector=crash,
)
"""
            child = run_crashing_child(code, root)
            self.assertEqual(73, child.returncode, child.stderr)
            self.assertNotEqual(before[first], first.read_bytes())
            self.assertEqual(before[second], second.read_bytes())
            pending = root / ".hvs-transactions/pending"
            self.assertTrue(pending.is_dir())

            with durable_batch_lock_and_recover(root):
                self.assertEqual(before[first], first.read_bytes())
                self.assertEqual(before[second], second.read_bytes())

            self.assertFalse(pending.exists())

    def test_all_after_recovery_fsyncs_file_and_parent_before_cleanup(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            target = root / "mapping.skills.json"
            target.write_text('{"value":"before"}\n', encoding="utf-8")
            code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import sync_upstream as sync
root = Path(sys.argv[1]); sync.REPO_ROOT = root
target = root / "mapping.skills.json"
def crash(event, path):
    if event == "after_replace" and path == target:
        os._exit(78)
sync.atomic_write_json_batch(
    {target: {"value": "after"}},
    fault_injector=crash,
)
"""
            child = run_crashing_child(code, root)
            self.assertEqual(78, child.returncode, child.stderr)
            self.assertEqual(
                {"value": "after"},
                json.loads(target.read_text(encoding="utf-8")),
            )
            pending = root / ".hvs-transactions/pending"
            self.assertTrue(pending.is_dir())
            target_identity = (target.stat().st_dev, target.stat().st_ino)
            parent_identity = (
                target.parent.stat().st_dev,
                target.parent.stat().st_ino,
            )
            flushed: set[tuple[int, int]] = set()
            real_fsync = os.fsync

            def recording_fsync(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                flushed.add((metadata.st_dev, metadata.st_ino))
                real_fsync(descriptor)

            with mock.patch(
                "scripts.durable_file_batch.os.fsync",
                side_effect=recording_fsync,
            ):
                with durable_batch_lock_and_recover(root):
                    pass

            self.assertIn(target_identity, flushed)
            self.assertIn(parent_identity, flushed)
            self.assertFalse(pending.exists())

    def test_all_after_shared_parent_flushes_once_after_every_file(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"before-one")
            second.write_bytes(b"before-two")
            apply_finished = False
            events: list[str] = []
            identities: dict[tuple[int, int], str] = {}
            real_fsync = os.fsync

            def apply() -> None:
                nonlocal apply_finished
                first.write_bytes(b"after-one")
                second.write_bytes(b"after-two")
                identities[
                    (first.stat().st_dev, first.stat().st_ino)
                ] = "first"
                identities[
                    (second.stat().st_dev, second.stat().st_ino)
                ] = "second"
                identities[
                    (root.stat().st_dev, root.stat().st_ino)
                ] = "parent"
                apply_finished = True

            def recording_fsync(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                label = identities.get((metadata.st_dev, metadata.st_ino))
                if apply_finished and label is not None:
                    events.append(label)
                real_fsync(descriptor)

            with mock.patch(
                "scripts.durable_file_batch.os.fsync",
                side_effect=recording_fsync,
            ):
                with durable_batch_lock_and_recover(root) as guard:
                    guard.commit_batch(
                        {
                            first: b"after-one",
                            second: b"after-two",
                        },
                        apply,
                    )

            self.assertEqual(["first", "second", "parent"], events)

    def test_all_after_parent_detach_preserves_journal_and_third_state(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            active_parent = root / "canonical"
            active_parent.mkdir()
            target = active_parent / "mapping.skills.json"
            target.write_text('{"value":"before"}\n', encoding="utf-8")
            code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import sync_upstream as sync
root = Path(sys.argv[1]); sync.REPO_ROOT = root
target = root / "canonical/mapping.skills.json"
def crash(event, path):
    if event == "after_replace" and path == target:
        os._exit(81)
sync.atomic_write_json_batch(
    {target: {"value": "after"}},
    fault_injector=crash,
)
"""
            child = run_crashing_child(code, root)
            self.assertEqual(81, child.returncode, child.stderr)
            pending = root / ".hvs-transactions/pending"
            self.assertTrue(pending.is_dir())
            parent_identity = (
                active_parent.stat().st_dev,
                active_parent.stat().st_ino,
            )
            detached_parent = root / "detached"
            detached = False
            real_fsync = os.fsync

            def detach_during_parent_fsync(descriptor: int) -> None:
                nonlocal detached
                metadata = os.fstat(descriptor)
                real_fsync(descriptor)
                if (
                    not detached
                    and (metadata.st_dev, metadata.st_ino)
                    == parent_identity
                ):
                    active_parent.rename(detached_parent)
                    active_parent.mkdir()
                    (active_parent / target.name).write_text(
                        "canonical-third-state",
                        encoding="utf-8",
                    )
                    detached = True

            with mock.patch(
                "scripts.durable_file_batch.os.fsync",
                side_effect=detach_during_parent_fsync,
            ):
                with self.assertRaises(DurableBatchRecoveryError) as raised:
                    with durable_batch_lock_and_recover(root):
                        pass

            self.assertTrue(detached)
            self.assertEqual((pending,), raised.exception.recovery_paths)
            self.assertEqual(
                "canonical-third-state",
                target.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {"value": "after"},
                json.loads(
                    (detached_parent / target.name).read_text(
                        encoding="utf-8"
                    )
                ),
            )
            self.assertTrue(pending.is_dir())

    def test_ingest_hard_exit_recovers_files_and_created_parents(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            existing = root / "z-existing.txt"
            existing.write_bytes(b"before-existing")
            new_target = root / "created" / "nested" / "first.txt"
            code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import ingest_skill as ingest
from durable_file_batch import durable_batch_lock_and_recover
root = Path(sys.argv[1])
new_target = root / "created" / "nested" / "first.txt"
existing = root / "z-existing.txt"
plans = [
    ingest.IngestPlan(
        skill_name="first",
        skill_md=new_target,
        before_skill=None,
        after_skill=b"after-new",
        repo_root=root,
        before_checkpoint=ingest.capture_target_checkpoint(
            new_target, repo_root=root
        ),
        before_parent_checkpoint=ingest.capture_parent_checkpoint(
            root, new_target
        ),
    ),
    ingest.IngestPlan(
        skill_name="existing",
        skill_md=existing,
        before_skill=b"before-existing",
        after_skill=b"after-existing",
        repo_root=root,
        before_checkpoint=ingest.capture_target_checkpoint(
            existing, repo_root=root
        ),
        before_parent_checkpoint=ingest.capture_parent_checkpoint(
            root, existing
        ),
    ),
]
def crash(event, path):
    if event == "after_replace" and path == new_target:
        os._exit(74)
with durable_batch_lock_and_recover(root) as guard:
    ingest.commit_ingest_plans(
        plans,
        locks_held=True,
        durable_guard=guard,
        fault_injector=crash,
    )
"""
            child = run_crashing_child(code, root)
            self.assertEqual(74, child.returncode, child.stderr)
            self.assertEqual(b"after-new", new_target.read_bytes())
            self.assertEqual(b"before-existing", existing.read_bytes())

            with durable_batch_lock_and_recover(root):
                self.assertFalse(new_target.exists())
                self.assertEqual(b"before-existing", existing.read_bytes())

            self.assertFalse((root / "created").exists())
            self.assertFalse((root / ".hvs-transactions/pending").exists())

    def test_third_state_is_never_overwritten_and_keeps_private_recovery(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            first = root / "first.skills.json"
            second = root / "second.skills.json"
            first.write_text('{"value":"before-one"}\n', encoding="utf-8")
            second.write_text('{"value":"before-two"}\n', encoding="utf-8")
            code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import sync_upstream as sync
root = Path(sys.argv[1]); sync.REPO_ROOT = root
first = root / "first.skills.json"; second = root / "second.skills.json"
def crash(event, path):
    if event == "after_replace" and path == first:
        os._exit(75)
sync.atomic_write_json_batch(
    {first: {"value": "after-one"}, second: {"value": "after-two"}},
    fault_injector=crash,
)
"""
            self.assertEqual(75, run_crashing_child(code, root).returncode)
            first.write_bytes(b"user-third-state")
            second_before = second.read_bytes()

            with self.assertRaises(DurableBatchRecoveryError):
                with durable_batch_lock_and_recover(root):
                    pass

            self.assertEqual(b"user-third-state", first.read_bytes())
            self.assertEqual(second_before, second.read_bytes())
            pending = root / ".hvs-transactions/pending"
            self.assertTrue(pending.is_dir())
            for path in pending.iterdir():
                self.assertEqual(0, stat.S_IMODE(path.stat().st_mode) & 0o077)

    def test_symlink_target_recovery_fails_closed_with_recovery_path(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            target = root / "target.txt"
            sentinel = root / "sentinel.txt"
            target.write_bytes(b"before")
            sentinel.write_bytes(b"sentinel")
            code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from durable_file_batch import durable_batch_lock_and_recover
root = Path(sys.argv[1]); target = root / "target.txt"
with durable_batch_lock_and_recover(root) as guard:
    guard.commit_batch({target: b"after"}, lambda: os._exit(79))
"""
            self.assertEqual(79, run_crashing_child(code, root).returncode)
            target.unlink()
            target.symlink_to(sentinel)
            pending = root / ".hvs-transactions/pending"

            with self.assertRaises(DurableBatchRecoveryError) as raised:
                with durable_batch_lock_and_recover(root):
                    pass

            self.assertEqual((pending,), raised.exception.recovery_paths)
            self.assertTrue(target.is_symlink())
            self.assertEqual(b"sentinel", sentinel.read_bytes())
            self.assertTrue(pending.is_dir())

    def test_tampered_or_symlinked_journal_fails_closed(self):
        for variant in ("tampered", "symlink"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(
                dir=REPO_ROOT
            ) as tmp:
                root = Path(tmp)
                target = root / "target.txt"
                target.write_bytes(b"before")
                code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from durable_file_batch import durable_batch_lock_and_recover
root = Path(sys.argv[1]); target = root / "target.txt"
with durable_batch_lock_and_recover(root) as guard:
    guard.commit_batch(
        {target: b"after"},
        lambda: os._exit(76),
    )
"""
                self.assertEqual(76, run_crashing_child(code, root).returncode)
                journal = root / ".hvs-transactions/pending/journal.json"
                sentinel = root / "sentinel"
                sentinel.write_text("do-not-read-or-change", encoding="utf-8")
                if variant == "tampered":
                    envelope = json.loads(journal.read_text(encoding="utf-8"))
                    envelope["checksum"] = "0" * 64
                    journal.write_text(json.dumps(envelope), encoding="utf-8")
                    journal.chmod(0o600)
                else:
                    journal.unlink()
                    journal.symlink_to(sentinel)

                with self.assertRaises((DurableBatchRecoveryError, OSError)):
                    with durable_batch_lock_and_recover(root):
                        pass

                self.assertEqual(b"before", target.read_bytes())
                self.assertEqual(
                    "do-not-read-or-change",
                    sentinel.read_text(encoding="utf-8"),
                )
                self.assertTrue(
                    (root / ".hvs-transactions/pending").exists()
                )

    def test_special_mode_is_rejected_before_journal_publication(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            target = root / "executable"
            target.write_bytes(b"before")
            target.chmod(0o4755)
            called = False

            def apply() -> None:
                nonlocal called
                called = True
                target.write_bytes(b"after")

            with self.assertRaisesRegex(
                DurableBatchError,
                "special bits",
            ):
                with durable_batch_lock_and_recover(root) as guard:
                    guard.commit_batch({target: b"after"}, apply)

            self.assertFalse(called)
            self.assertEqual(b"before", target.read_bytes())
            self.assertEqual(0o4755, stat.S_IMODE(target.stat().st_mode))
            self.assertFalse(
                (root / ".hvs-transactions/pending").exists()
            )

    def test_v2_apply_dry_run_does_not_create_transaction_state(self):
        import scripts.sync_upstream as sync

        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            mapping = root / "mapping.skills.json"
            mapping.write_text("{}\n", encoding="utf-8")
            local = root / "skills/demo/SKILL.md"
            local.parent.mkdir(parents=True)
            local.write_text("# Demo\n", encoding="utf-8")
            skill = {
                "tracking": {"channel": "latest_release"},
                "sync_mode": "replace",
                "mapping_path": mapping,
                "origin_index": 0,
                "repo_skill": "skills/demo/SKILL.md",
                "local_path": local,
            }
            update = {
                "skill": skill,
                "current_commit": "a" * 40,
                "path_commit": "b" * 40,
                "resolved_ref": "v1.0.0",
            }
            entry = {"origins": [{"tracking": {}}], "managed_files": []}
            origin = entry["origins"][0]

            @contextmanager
            def no_op_mapping_lock(_path):
                yield

            engine = SimpleNamespace(
                plan_artifact_set_sync=lambda *_args, **_kwargs: object(),
                apply_artifact_set_sync=lambda _plan, *, dry_run: (
                    "dry-result" if dry_run else None
                ),
            )
            before = tree_digest(root)
            old_root = sync.REPO_ROOT
            sync.REPO_ROOT = root
            try:
                with (
                    mock.patch.object(
                        sync,
                        "durable_batch_lock_and_recover",
                        side_effect=AssertionError(
                            "dry-run must not enter durable write recovery"
                        ),
                    ),
                    mock.patch.object(
                        sync,
                        "mapping_advisory_lock",
                        side_effect=no_op_mapping_lock,
                    ),
                    mock.patch.object(
                        sync,
                        "_v2_entry_and_origin",
                        return_value=(entry, origin),
                    ),
                    mock.patch.object(
                        sync,
                        "_load_artifact_engine",
                        return_value=engine,
                    ),
                    mock.patch.object(
                        sync,
                        "_artifact_payloads_for_engine",
                        return_value=[],
                    ),
                    mock.patch.object(
                        sync,
                        "_other_origin_target_conflicts",
                        return_value=[],
                    ),
                ):
                    self.assertEqual(
                        "dry-result",
                        sync.apply_v2_update(update, dry_run=True),
                    )
            finally:
                sync.REPO_ROOT = old_root
            self.assertEqual(before, tree_digest(root))
            self.assertFalse((root / ".hvs-transactions").exists())

    def test_ingest_dry_lock_path_is_repository_read_only(self):
        import scripts.ingest_skill as ingest

        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            sources = root / "docs/sources"
            sources.mkdir(parents=True)
            mapping = sources / "source.skills.json"
            mapping.write_text('{"schema_version":2,"skills":[]}\n')
            skill = root / "skills/category/demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Demo\n")
            before = tree_digest(root)

            with ingest.acquire_ingest_locks(
                repo_root=root,
                skill_dirs=[skill],
                mapping_paths=[mapping],
                durable=False,
            ) as guard:
                self.assertIsNone(guard)

            self.assertEqual(before, tree_digest(root))
            self.assertFalse((root / ".hvs-transactions").exists())

    def test_ingest_dry_lock_does_not_recover_real_artifact_journal(self):
        import scripts.artifact_set_sync as artifact_sync
        import scripts.ingest_skill as ingest

        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmp:
            root = Path(tmp)
            sources = root / "docs/sources"
            sources.mkdir(parents=True)
            mapping = sources / "source.skills.json"
            mapping.write_text('{"schema_version":2,"skills":[]}\n')
            skill_root = "skills/category/demo"
            skill_dir = root / skill_root
            skill_dir.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_bytes(b"old body")
            code = r"""
import hashlib, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from artifact_set_sync import plan_artifact_set_sync, prepare_artifact_set_sync
root = Path(sys.argv[1])
skill = "skills/category/demo/SKILL.md"
old = (root / skill).read_bytes()
entry = {
    "normalized_slug": "demo",
    "repo_skill": skill,
    "kind": "mirror",
    "origins": [{"artifacts": [{
        "source": "release/demo",
        "target": "skills/category/demo",
        "type": "directory",
    }]}],
    "managed_files": [{
        "path": skill,
        "sha256": hashlib.sha256(old).hexdigest(),
        "owner": "demo",
    }],
}
plan = plan_artifact_set_sync(
    root,
    entry,
    [{
        "source": "release/SKILL.md",
        "target": skill,
        "type": "file",
        "data": b"new body",
    }],
    {"ref": "v2"},
)
prepare_artifact_set_sync(plan)
os._exit(80)
"""
            child = run_crashing_child(code, root)
            self.assertEqual(80, child.returncode, child.stderr)
            journal = artifact_sync.skill_transaction_journal_path(
                root,
                skill_root,
            )
            self.assertTrue(journal.exists())
            before = tree_digest(root)

            with self.assertRaises(Exception) as raised:
                with ingest.acquire_ingest_locks(
                    repo_root=root,
                    skill_dirs=[skill_dir],
                    mapping_paths=[mapping],
                    durable=False,
                ):
                    pass

            self.assertEqual(
                "ArtifactRecoveryError",
                type(raised.exception).__name__,
            )
            self.assertEqual((journal,), raised.exception.recovery_paths)
            self.assertEqual(before, tree_digest(root))
            self.assertTrue(journal.exists())

            # Explicit writable entry recovers the fixture for test cleanup.
            with artifact_sync.skill_advisory_lock(root, skill_root):
                pass


if __name__ == "__main__":
    unittest.main()

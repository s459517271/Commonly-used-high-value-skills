#!/usr/bin/env python3
"""Crash-durable coordination for multi-file repository replacements.

The public entrypoint is :func:`durable_batch_lock_and_recover`.  Every writer
uses one repository-scoped lock and publishes a private, fsync-backed journal
before its first destination replacement.  A later process classifies the
whole batch as all-before, all-after, a safe before/after mixture, or an
unknown third state.  Only the safe mixture is rolled back automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - repository CI is POSIX
    fcntl = None  # type: ignore[assignment]


STATE_DIRECTORY = ".hvs-transactions"
LOCK_FILENAME = "batch.lock"
PENDING_DIRECTORY = "pending"
JOURNAL_FILENAME = "journal.json"
JOURNAL_VERSION = 1
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
READ_CHUNK = 1024 * 1024


class DurableBatchError(RuntimeError):
    """A durable batch cannot proceed without risking user-owned bytes."""

    def __init__(
        self,
        message: str,
        *,
        recovery_paths: tuple[Path, ...] = (),
    ) -> None:
        self.recovery_paths = recovery_paths
        suffix = (
            "; recovery=" + ", ".join(str(path) for path in recovery_paths)
            if recovery_paths
            else ""
        )
        super().__init__(message + suffix)


class DurableBatchLockError(DurableBatchError):
    """The repository-wide durable batch lock is unavailable or unsafe."""


class DurableBatchRecoveryError(DurableBatchError):
    """A pending journal is unsafe, tampered, or contains a third state."""


@dataclass(frozen=True)
class FileFingerprint:
    exists: bool
    sha256: str | None
    mode: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "sha256": self.sha256,
            "mode": self.mode,
        }


def _uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _validate_owned_directory(
    descriptor: int,
    path: Path,
    *,
    private: bool,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    named = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or metadata.st_dev != named.st_dev
        or metadata.st_ino != named.st_ino
        or (_uid() is not None and metadata.st_uid != _uid())
    ):
        raise DurableBatchError(f"unsafe durable batch directory: {path}")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DurableBatchError(
            f"durable batch directory is not private: {path}"
        )
    return metadata


def _repository_root(repo_root: Path) -> Path:
    root = Path(os.path.abspath(repo_root))
    descriptor = os.open(root, _directory_flags())
    try:
        _validate_owned_directory(descriptor, root, private=False)
    finally:
        os.close(descriptor)
    return root


def _ensure_state_root(repo_root: Path) -> tuple[Path, int]:
    state_root = repo_root / STATE_DIRECTORY
    root_fd = os.open(repo_root, _directory_flags())
    try:
        try:
            os.mkdir(STATE_DIRECTORY, 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        state_fd = os.open(STATE_DIRECTORY, _directory_flags(), dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        _validate_owned_directory(state_fd, state_root, private=True)
    except BaseException:
        os.close(state_fd)
        raise
    return state_root, state_fd


def _validate_lock_descriptor(
    state_fd: int,
    descriptor: int,
    lock_path: Path,
) -> None:
    metadata = os.fstat(descriptor)
    named = os.stat(
        LOCK_FILENAME,
        dir_fd=state_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or metadata.st_dev != named.st_dev
        or metadata.st_ino != named.st_ino
        or (_uid() is not None and metadata.st_uid != _uid())
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise DurableBatchLockError(
            f"unsafe durable batch lock: {lock_path}"
        )


def _relative_target(repo_root: Path, target: Path) -> str:
    candidate = Path(os.path.abspath(target))
    try:
        relative = candidate.relative_to(repo_root)
    except ValueError as exc:
        raise DurableBatchError(
            f"durable batch target escapes repository root: {target}"
        ) from exc
    value = relative.as_posix()
    pure = PurePosixPath(value)
    if (
        not value
        or pure == PurePosixPath(".")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in value
    ):
        raise DurableBatchError(f"unsafe durable batch target: {target}")
    if pure.parts[0] == STATE_DIRECTORY:
        raise DurableBatchError(
            "durable batch targets must not overlap transaction state"
        )
    return value


def _open_target_parent(
    repo_root: Path,
    relative: str,
    *,
    allow_missing: bool,
) -> tuple[int | None, str]:
    parts = PurePosixPath(relative).parts
    descriptor = os.open(repo_root, _directory_flags())
    try:
        for component in parts[:-1]:
            try:
                next_descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if allow_missing:
                    os.close(descriptor)
                    return None, parts[-1]
                raise
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise DurableBatchError(
                    f"target ancestor is not a directory: {relative}"
                )
        return descriptor, parts[-1]
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _missing_parent_paths(repo_root: Path, relative: str) -> list[str]:
    """Return parent directories absent before the batch, shallow to deep."""
    parts = PurePosixPath(relative).parts[:-1]
    descriptor = os.open(repo_root, _directory_flags())
    missing: list[str] = []
    prefix: list[str] = []
    absent = False
    try:
        for component in parts:
            prefix.append(component)
            if absent:
                missing.append(PurePosixPath(*prefix).as_posix())
                continue
            try:
                next_descriptor = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                absent = True
                missing.append(PurePosixPath(*prefix).as_posix())
                continue
            os.close(descriptor)
            descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise DurableBatchError(
                    f"target ancestor is not a directory: {relative}"
                )
        return missing
    finally:
        os.close(descriptor)


def _read_all(descriptor: int, *, limit: int | None = None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if limit is not None and total > limit:
            raise DurableBatchRecoveryError("durable journal exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _capture_target(
    repo_root: Path,
    relative: str,
    *,
    include_content: bool,
    recovery_path: Path | None = None,
) -> tuple[FileFingerprint, bytes | None]:
    try:
        parent_fd, leaf = _open_target_parent(
            repo_root,
            relative,
            allow_missing=True,
        )
    except (OSError, DurableBatchError) as exc:
        if recovery_path is not None:
            raise DurableBatchRecoveryError(
                f"cannot safely open durable recovery target: {relative}",
                recovery_paths=(recovery_path,),
            ) from exc
        raise
    if parent_fd is None:
        return FileFingerprint(False, None, None), None
    descriptor = -1
    try:
        try:
            descriptor = os.open(leaf, _file_read_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            return FileFingerprint(False, None, None), None
        except OSError as exc:
            if recovery_path is not None:
                raise DurableBatchRecoveryError(
                    f"durable recovery target is missing or unsafe: {relative}",
                    recovery_paths=(recovery_path,),
                ) from exc
            raise
        metadata = os.fstat(descriptor)
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or metadata.st_dev != named.st_dev
            or metadata.st_ino != named.st_ino
        ):
            error_type = (
                DurableBatchRecoveryError
                if recovery_path is not None
                else DurableBatchError
            )
            raise error_type(
                f"durable batch target is not a stable regular file: {relative}",
                recovery_paths=(recovery_path,) if recovery_path else (),
            )
        content = _read_all(descriptor)
        fingerprint = FileFingerprint(
            True,
            hashlib.sha256(content).hexdigest(),
            stat.S_IMODE(metadata.st_mode),
        )
        return fingerprint, content if include_content else None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _fingerprint_from_json(value: object, *, label: str) -> FileFingerprint:
    if not isinstance(value, dict) or set(value) != {
        "exists",
        "sha256",
        "mode",
    }:
        raise DurableBatchRecoveryError(f"invalid {label} fingerprint")
    exists = value.get("exists")
    digest = value.get("sha256")
    mode = value.get("mode")
    if type(exists) is not bool:
        raise DurableBatchRecoveryError(f"invalid {label}.exists")
    if exists:
        if (
            not isinstance(digest, str)
            or not re_full_sha256(digest)
            or isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o777
        ):
            raise DurableBatchRecoveryError(f"invalid {label} file state")
    elif digest is not None or mode is not None:
        raise DurableBatchRecoveryError(f"invalid absent {label} state")
    return FileFingerprint(exists, digest, mode)


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_private_file(
    directory_fd: int,
    name: str,
    content: bytes,
) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or metadata.st_dev != named.st_dev
            or metadata.st_ino != named.st_ino
        ):
            raise DurableBatchError(
                f"private durable batch file changed while writing: {name}"
            )
    finally:
        os.close(descriptor)


def _persist_journal(
    repo_root: Path,
    state_root: Path,
    state_fd: int,
    replacements: Mapping[Path, bytes | None],
    after_modes: Mapping[Path, int | None] | None,
) -> dict[str, Any]:
    transaction_name = f".pending.{secrets.token_hex(16)}"
    os.mkdir(transaction_name, 0o700, dir_fd=state_fd)
    os.fsync(state_fd)
    transaction_path = state_root / transaction_name
    transaction_fd = os.open(
        transaction_name,
        _directory_flags(),
        dir_fd=state_fd,
    )
    published = False
    try:
        _validate_owned_directory(
            transaction_fd,
            transaction_path,
            private=True,
        )
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        missing_directories: set[str] = set()
        for index, (target, desired) in enumerate(
            sorted(replacements.items(), key=lambda item: str(item[0]))
        ):
            relative = _relative_target(repo_root, Path(target))
            if relative in seen:
                raise DurableBatchError(
                    f"duplicate durable batch target: {relative}"
                )
            seen.add(relative)
            missing_directories.update(
                _missing_parent_paths(repo_root, relative)
            )
            before, original = _capture_target(
                repo_root,
                relative,
                include_content=True,
            )
            if before.exists and (
                before.mode is None or before.mode < 0 or before.mode > 0o777
            ):
                raise DurableBatchError(
                    "durable batch refuses target mode with special bits: "
                    f"{relative}"
                )
            requested_mode = (
                (after_modes or {}).get(Path(target))
                if after_modes is not None
                else None
            )
            if requested_mode is not None and (
                isinstance(requested_mode, bool)
                or not isinstance(requested_mode, int)
                or requested_mode < 0
                or requested_mode > 0o777
            ):
                raise DurableBatchError(
                    f"invalid durable after mode for {relative}"
                )
            after = FileFingerprint(False, None, None)
            if desired is not None:
                after = FileFingerprint(
                    True,
                    hashlib.sha256(desired).hexdigest(),
                    (
                        requested_mode
                        if requested_mode is not None
                        else before.mode
                        if before.exists
                        else 0o644
                    ),
                )
            payload_name = None
            if before.exists:
                payload_name = f"{index:08d}.before"
                if original is None:
                    raise DurableBatchError(
                        f"missing original bytes for {relative}"
                    )
                _write_private_file(transaction_fd, payload_name, original)
            entries.append(
                {
                    "path": relative,
                    "before": before.as_json(),
                    "after": after.as_json(),
                    "before_payload": payload_name,
                }
            )
        payload = {
            "version": JOURNAL_VERSION,
            "repository": hashlib.sha256(
                str(repo_root).encode("utf-8")
            ).hexdigest(),
            "entries": entries,
            "created_directories": sorted(
                missing_directories,
                key=lambda value: (
                    len(PurePosixPath(value).parts),
                    value,
                ),
            ),
        }
        payload_bytes = _canonical_json(payload)
        envelope = {
            "payload": payload,
            "checksum": hashlib.sha256(payload_bytes).hexdigest(),
        }
        journal_bytes = _canonical_json(envelope) + b"\n"
        if len(journal_bytes) > MAX_JOURNAL_BYTES:
            raise DurableBatchError("durable batch journal exceeds size limit")
        _write_private_file(
            transaction_fd,
            f".{JOURNAL_FILENAME}.tmp",
            journal_bytes,
        )
        os.rename(
            f".{JOURNAL_FILENAME}.tmp",
            JOURNAL_FILENAME,
            src_dir_fd=transaction_fd,
            dst_dir_fd=transaction_fd,
        )
        os.fsync(transaction_fd)
        try:
            os.rename(
                transaction_name,
                PENDING_DIRECTORY,
                src_dir_fd=state_fd,
                dst_dir_fd=state_fd,
            )
        except FileExistsError as exc:
            raise DurableBatchRecoveryError(
                "another durable batch journal is already pending",
                recovery_paths=(state_root / PENDING_DIRECTORY,),
            ) from exc
        os.fsync(state_fd)
        published = True
        return payload
    finally:
        os.close(transaction_fd)
        if not published:
            _remove_unpublished_transaction(
                state_fd,
                transaction_name,
            )


def _remove_unpublished_transaction(state_fd: int, name: str) -> None:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=state_fd)
    except FileNotFoundError:
        return
    try:
        for child in os.listdir(descriptor):
            try:
                os.unlink(child, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=state_fd)
        os.fsync(state_fd)
    except FileNotFoundError:
        pass


def _read_private_file(
    directory_fd: int,
    name: str,
    *,
    limit: int | None = None,
) -> bytes:
    descriptor = os.open(name, _file_read_flags(), dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or metadata.st_dev != named.st_dev
            or metadata.st_ino != named.st_ino
            or (_uid() is not None and metadata.st_uid != _uid())
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise DurableBatchRecoveryError(
                f"unsafe durable recovery file: {name}"
            )
        content = _read_all(descriptor, limit=limit)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
        ):
            raise DurableBatchRecoveryError(
                f"durable recovery file changed while reading: {name}"
            )
        return content
    finally:
        os.close(descriptor)


def _load_pending(
    repo_root: Path,
    state_root: Path,
    state_fd: int,
) -> tuple[dict[str, Any], dict[str, bytes], int] | None:
    try:
        pending_fd = os.open(
            PENDING_DIRECTORY,
            _directory_flags(),
            dir_fd=state_fd,
        )
    except FileNotFoundError:
        return None
    pending_path = state_root / PENDING_DIRECTORY
    try:
        _validate_owned_directory(pending_fd, pending_path, private=True)
        try:
            raw = _read_private_file(
                pending_fd,
                JOURNAL_FILENAME,
                limit=MAX_JOURNAL_BYTES,
            )
        except OSError as exc:
            raise DurableBatchRecoveryError(
                "durable batch journal is missing or unsafe",
                recovery_paths=(pending_path,),
            ) from exc
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DurableBatchRecoveryError(
                "durable batch journal is not valid UTF-8 JSON",
                recovery_paths=(pending_path,),
            ) from exc
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"payload", "checksum"}
            or not isinstance(envelope.get("payload"), dict)
            or not isinstance(envelope.get("checksum"), str)
        ):
            raise DurableBatchRecoveryError(
                "durable batch journal envelope is invalid",
                recovery_paths=(pending_path,),
            )
        payload = envelope["payload"]
        expected_checksum = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if envelope["checksum"] != expected_checksum:
            raise DurableBatchRecoveryError(
                "durable batch journal checksum mismatch",
                recovery_paths=(pending_path,),
            )
        if (
            set(payload)
            != {
                "version",
                "repository",
                "entries",
                "created_directories",
            }
            or payload.get("version") != JOURNAL_VERSION
            or payload.get("repository")
            != hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()
            or not isinstance(payload.get("entries"), list)
            or not payload["entries"]
            or not isinstance(payload.get("created_directories"), list)
        ):
            raise DurableBatchRecoveryError(
                "durable batch journal authority is invalid",
                recovery_paths=(pending_path,),
            )
        originals: dict[str, bytes] = {}
        seen: set[str] = set()
        directories: list[str] = []
        for value in payload["created_directories"]:
            if (
                not isinstance(value, str)
                or _relative_target(repo_root, repo_root / value) != value
                or value in directories
                or value == STATE_DIRECTORY
                or value.startswith(STATE_DIRECTORY + "/")
            ):
                raise DurableBatchRecoveryError(
                    "durable batch created-directory list is invalid",
                    recovery_paths=(pending_path,),
                )
            directories.append(value)
        if directories != sorted(
            directories,
            key=lambda value: (len(PurePosixPath(value).parts), value),
        ):
            raise DurableBatchRecoveryError(
                "durable batch created-directory list is not canonical",
                recovery_paths=(pending_path,),
            )
        expected_children = {JOURNAL_FILENAME}
        for index, entry in enumerate(payload["entries"]):
            if not isinstance(entry, dict) or set(entry) != {
                "path",
                "before",
                "after",
                "before_payload",
            }:
                raise DurableBatchRecoveryError(
                    "durable batch journal entry is invalid",
                    recovery_paths=(pending_path,),
                )
            relative = entry.get("path")
            if (
                not isinstance(relative, str)
                or _relative_target(repo_root, repo_root / relative) != relative
                or relative in seen
            ):
                raise DurableBatchRecoveryError(
                    "durable batch journal path is invalid or duplicated",
                    recovery_paths=(pending_path,),
                )
            seen.add(relative)
            before = _fingerprint_from_json(
                entry.get("before"),
                label=f"entries[{index}].before",
            )
            _fingerprint_from_json(
                entry.get("after"),
                label=f"entries[{index}].after",
            )
            payload_name = entry.get("before_payload")
            if before.exists:
                if (
                    not isinstance(payload_name, str)
                    or payload_name != f"{index:08d}.before"
                ):
                    raise DurableBatchRecoveryError(
                        "durable batch original payload name is invalid",
                        recovery_paths=(pending_path,),
                    )
                original = _read_private_file(pending_fd, payload_name)
                if hashlib.sha256(original).hexdigest() != before.sha256:
                    raise DurableBatchRecoveryError(
                        "durable batch original payload hash mismatch",
                        recovery_paths=(pending_path,),
                    )
                originals[relative] = original
                expected_children.add(payload_name)
            elif payload_name is not None:
                raise DurableBatchRecoveryError(
                    "absent durable batch target unexpectedly has a payload",
                    recovery_paths=(pending_path,),
                )
        if set(os.listdir(pending_fd)) != expected_children:
            raise DurableBatchRecoveryError(
                "durable batch pending directory contains unknown files",
                recovery_paths=(pending_path,),
            )
        return payload, originals, pending_fd
    except BaseException:
        os.close(pending_fd)
        raise


def _matches(current: FileFingerprint, expected: FileFingerprint) -> bool:
    return current == expected


def _replace_target_with_original(
    repo_root: Path,
    relative: str,
    content: bytes,
    mode: int,
) -> None:
    parent_fd, leaf = _open_target_parent(
        repo_root,
        relative,
        allow_missing=False,
    )
    if parent_fd is None:  # pragma: no cover - allow_missing is false
        raise DurableBatchRecoveryError(
            f"recovery parent is missing: {relative}"
        )
    temporary = f".{leaf}.{secrets.token_hex(16)}.recovery.tmp"
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.rename(
            temporary,
            leaf,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        created = False
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _remove_after_target(repo_root: Path, relative: str) -> None:
    parent_fd, leaf = _open_target_parent(
        repo_root,
        relative,
        allow_missing=False,
    )
    if parent_fd is None:  # pragma: no cover - allow_missing is false
        raise DurableBatchRecoveryError(
            f"recovery parent is missing: {relative}"
        )
    try:
        os.unlink(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _cleanup_pending(
    state_root: Path,
    state_fd: int,
    pending_fd: int,
    payload: dict[str, Any],
) -> None:
    names = {JOURNAL_FILENAME}
    for entry in payload["entries"]:
        if entry.get("before_payload") is not None:
            names.add(entry["before_payload"])
    if set(os.listdir(pending_fd)) != names:
        raise DurableBatchRecoveryError(
            "durable batch pending directory changed before cleanup",
            recovery_paths=(state_root / PENDING_DIRECTORY,),
        )
    for name in sorted(names):
        os.unlink(name, dir_fd=pending_fd)
    os.fsync(pending_fd)
    os.close(pending_fd)
    os.rmdir(PENDING_DIRECTORY, dir_fd=state_fd)
    os.fsync(state_fd)


def _cleanup_created_directories(
    repo_root: Path,
    payload: dict[str, Any],
    *,
    recovery_path: Path,
) -> None:
    """Remove only parents that were absent pre-batch and remain empty."""
    for relative in reversed(payload["created_directories"]):
        pure = PurePosixPath(relative)
        parent_relative = (
            PurePosixPath(*pure.parts[:-1]).as_posix()
            if len(pure.parts) > 1
            else None
        )
        if parent_relative is None:
            parent_fd = os.open(repo_root, _directory_flags())
        else:
            parent_fd, _unused = _open_target_parent(
                repo_root,
                parent_relative + "/sentinel",
                allow_missing=True,
            )
            if parent_fd is None:
                continue
        try:
            try:
                metadata = os.stat(
                    pure.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise DurableBatchRecoveryError(
                    f"created recovery parent became unsafe: {relative}",
                    recovery_paths=(recovery_path,),
                )
            try:
                os.rmdir(pure.name, dir_fd=parent_fd)
            except OSError as exc:
                raise DurableBatchRecoveryError(
                    f"created recovery parent is not empty: {relative}",
                    recovery_paths=(recovery_path,),
                ) from exc
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _open_verified_active_parent(
    repo_root: Path,
    relative: str,
    expected_identity: tuple[int, int],
    *,
    recovery_path: Path,
) -> tuple[int, str]:
    """Reopen the canonical parent path and prove it is still the pinned inode."""
    try:
        parent_fd, leaf = _open_target_parent(
            repo_root,
            relative,
            allow_missing=False,
        )
    except (OSError, DurableBatchError) as exc:
        raise DurableBatchRecoveryError(
            f"cannot reopen active recovery parent: {relative}",
            recovery_paths=(recovery_path,),
        ) from exc
    if parent_fd is None:  # pragma: no cover - allow_missing is false
        raise DurableBatchRecoveryError(
            f"active recovery parent is missing: {relative}",
            recovery_paths=(recovery_path,),
        )
    metadata = os.fstat(parent_fd)
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        os.close(parent_fd)
        raise DurableBatchRecoveryError(
            f"active recovery parent detached from canonical path: {relative}",
            recovery_paths=(recovery_path,),
        )
    return parent_fd, leaf


def _verify_active_parent_identity(
    repo_root: Path,
    relative: str,
    expected_identity: tuple[int, int],
    *,
    recovery_path: Path,
) -> None:
    parent_fd, _leaf = _open_verified_active_parent(
        repo_root,
        relative,
        expected_identity,
        recovery_path=recovery_path,
    )
    os.close(parent_fd)


def _confirm_after_states_durable(
    repo_root: Path,
    payload: dict[str, Any],
    *,
    recovery_path: Path,
) -> None:
    """Revalidate and durably flush every installed after state.

    An atomic rename can become visible before either its file data or parent
    directory entry is durable.  A process recovering an all-after journal
    therefore assumes responsibility for both fsync boundaries before it may
    discard the only rollback authority.
    """
    parent_fds: dict[tuple[int, int], int] = {}
    parent_authorities: dict[tuple[int, int], str] = {}
    checks: list[
        tuple[tuple[int, int], str, str, FileFingerprint]
    ] = []
    try:
        # Flush every regular file first.  Parent directory fsyncs happen only
        # after all file-data boundaries, preserving the required ordering
        # even when several targets share one parent.
        for entry in payload["entries"]:
            relative = entry["path"]
            after = _fingerprint_from_json(entry["after"], label="after")
            try:
                opened_parent_fd, leaf = _open_target_parent(
                    repo_root,
                    relative,
                    allow_missing=False,
                )
            except (OSError, DurableBatchError) as exc:
                raise DurableBatchRecoveryError(
                    f"cannot open all-after parent durably: {relative}",
                    recovery_paths=(recovery_path,),
                ) from exc
            if opened_parent_fd is None:  # pragma: no cover - missing forbidden
                raise DurableBatchRecoveryError(
                    f"all-after parent is missing: {relative}",
                    recovery_paths=(recovery_path,),
                )
            parent_metadata = os.fstat(opened_parent_fd)
            parent_identity = (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            )
            parent_fd = parent_fds.get(parent_identity)
            if parent_fd is None:
                parent_fd = opened_parent_fd
                parent_fds[parent_identity] = parent_fd
                parent_authorities[parent_identity] = relative
            else:
                os.close(opened_parent_fd)
            descriptor = -1
            try:
                if after.exists:
                    try:
                        descriptor = os.open(
                            leaf,
                            _file_read_flags(),
                            dir_fd=parent_fd,
                        )
                    except OSError as exc:
                        raise DurableBatchRecoveryError(
                            f"cannot open all-after file durably: {relative}",
                            recovery_paths=(recovery_path,),
                        ) from exc
                    opened = os.fstat(descriptor)
                    named = os.stat(
                        leaf,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or stat.S_ISLNK(named.st_mode)
                        or opened.st_dev != named.st_dev
                        or opened.st_ino != named.st_ino
                    ):
                        raise DurableBatchRecoveryError(
                            "all-after file is not a stable regular file: "
                            f"{relative}",
                            recovery_paths=(recovery_path,),
                        )
                    content = _read_all(descriptor)
                    observed = FileFingerprint(
                        True,
                        hashlib.sha256(content).hexdigest(),
                        stat.S_IMODE(opened.st_mode),
                    )
                    if observed != after:
                        raise DurableBatchRecoveryError(
                            f"all-after file changed before durability: {relative}",
                            recovery_paths=(recovery_path,),
                        )
                    os.fsync(descriptor)
                    completed = os.fstat(descriptor)
                    renamed = os.stat(
                        leaf,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        completed.st_dev != opened.st_dev
                        or completed.st_ino != opened.st_ino
                        or completed.st_mode != opened.st_mode
                        or completed.st_size != opened.st_size
                        or completed.st_mtime_ns != opened.st_mtime_ns
                        or stat.S_ISLNK(renamed.st_mode)
                        or renamed.st_dev != opened.st_dev
                        or renamed.st_ino != opened.st_ino
                    ):
                        raise DurableBatchRecoveryError(
                            f"all-after file changed while being flushed: {relative}",
                            recovery_paths=(recovery_path,),
                        )
                    _verify_active_parent_identity(
                        repo_root,
                        relative,
                        parent_identity,
                        recovery_path=recovery_path,
                    )
                else:
                    try:
                        os.stat(
                            leaf,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise DurableBatchRecoveryError(
                            f"all-after deletion is no longer absent: {relative}",
                            recovery_paths=(recovery_path,),
                        )
                    _verify_active_parent_identity(
                        repo_root,
                        relative,
                        parent_identity,
                        recovery_path=recovery_path,
                    )
                checks.append(
                    (parent_identity, leaf, relative, after)
                )
            except DurableBatchRecoveryError:
                raise
            except OSError as exc:
                raise DurableBatchRecoveryError(
                    f"cannot confirm all-after file durability: {relative}",
                    recovery_paths=(recovery_path,),
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

        for parent_identity, parent_fd in parent_fds.items():
            os.fsync(parent_fd)
            _verify_active_parent_identity(
                repo_root,
                parent_authorities[parent_identity],
                parent_identity,
                recovery_path=recovery_path,
            )

        # Reopen every active parent from the repository root and use that
        # descriptor for the final target read.  The earlier pinned dirfd is
        # insufficient if its directory was renamed out of the canonical tree.
        for parent_identity, _leaf, relative, after in checks:
            active_parent_fd, active_leaf = _open_verified_active_parent(
                repo_root,
                relative,
                parent_identity,
                recovery_path=recovery_path,
            )
            try:
                if after.exists:
                    descriptor = -1
                    try:
                        try:
                            descriptor = os.open(
                                active_leaf,
                                _file_read_flags(),
                                dir_fd=active_parent_fd,
                            )
                        except OSError as exc:
                            raise DurableBatchRecoveryError(
                                f"cannot open all-after file durably: {relative}",
                                recovery_paths=(recovery_path,),
                            ) from exc
                        opened = os.fstat(descriptor)
                        named = os.stat(
                            active_leaf,
                            dir_fd=active_parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or stat.S_ISLNK(named.st_mode)
                            or opened.st_dev != named.st_dev
                            or opened.st_ino != named.st_ino
                        ):
                            raise DurableBatchRecoveryError(
                                "all-after file is not stable after parent flush: "
                                f"{relative}",
                                recovery_paths=(recovery_path,),
                            )
                        content = _read_all(descriptor)
                        completed = os.fstat(descriptor)
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    observed = FileFingerprint(
                        True,
                        hashlib.sha256(content).hexdigest(),
                        stat.S_IMODE(completed.st_mode),
                    )
                    if (
                        observed != after
                        or completed.st_dev != opened.st_dev
                        or completed.st_ino != opened.st_ino
                        or completed.st_mode != opened.st_mode
                        or completed.st_size != opened.st_size
                        or completed.st_mtime_ns != opened.st_mtime_ns
                    ):
                        raise DurableBatchRecoveryError(
                            f"all-after file changed after parent flush: {relative}",
                            recovery_paths=(recovery_path,),
                        )
                else:
                    try:
                        os.stat(
                            active_leaf,
                            dir_fd=active_parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise DurableBatchRecoveryError(
                            f"all-after deletion is no longer absent: {relative}",
                            recovery_paths=(recovery_path,),
                        )
            finally:
                os.close(active_parent_fd)
            _verify_active_parent_identity(
                repo_root,
                relative,
                parent_identity,
                recovery_path=recovery_path,
            )
    except DurableBatchRecoveryError:
        raise
    except OSError as exc:
        raise DurableBatchRecoveryError(
            "cannot confirm all-after durability",
            recovery_paths=(recovery_path,),
        ) from exc
    finally:
        for parent_fd in parent_fds.values():
            os.close(parent_fd)


def _recover_pending_locked(
    repo_root: Path,
    state_root: Path,
    state_fd: int,
) -> str:
    loaded = _load_pending(repo_root, state_root, state_fd)
    if loaded is None:
        return "none"
    payload, originals, pending_fd = loaded
    pending_path = state_root / PENDING_DIRECTORY
    try:
        states: list[str] = []
        for entry in payload["entries"]:
            current, _ = _capture_target(
                repo_root,
                entry["path"],
                include_content=False,
                recovery_path=pending_path,
            )
            before = _fingerprint_from_json(
                entry["before"],
                label="before",
            )
            after = _fingerprint_from_json(
                entry["after"],
                label="after",
            )
            if _matches(current, before):
                states.append("before")
            elif _matches(current, after):
                states.append("after")
            else:
                raise DurableBatchRecoveryError(
                    "durable batch contains an unknown third-state target: "
                    f"{entry['path']}",
                    recovery_paths=(pending_path,),
                )
        if all(value == "before" for value in states):
            _cleanup_created_directories(
                repo_root,
                payload,
                recovery_path=pending_path,
            )
            _cleanup_pending(
                state_root,
                state_fd,
                pending_fd,
                payload,
            )
            return "discarded-before"
        if all(value == "after" for value in states):
            _confirm_after_states_durable(
                repo_root,
                payload,
                recovery_path=pending_path,
            )
            _cleanup_pending(
                state_root,
                state_fd,
                pending_fd,
                payload,
            )
            return "completed-after"

        for entry, current_state in zip(payload["entries"], states, strict=True):
            if current_state != "after":
                continue
            before = _fingerprint_from_json(entry["before"], label="before")
            if before.exists:
                original = originals.get(entry["path"])
                if original is None or before.mode is None:
                    raise DurableBatchRecoveryError(
                        f"durable original is unavailable: {entry['path']}",
                        recovery_paths=(pending_path,),
                    )
                _replace_target_with_original(
                    repo_root,
                    entry["path"],
                    original,
                    before.mode,
                )
            else:
                _remove_after_target(repo_root, entry["path"])
        for entry in payload["entries"]:
            current, _ = _capture_target(
                repo_root,
                entry["path"],
                include_content=False,
                recovery_path=pending_path,
            )
            before = _fingerprint_from_json(entry["before"], label="before")
            if current != before:
                raise DurableBatchRecoveryError(
                    f"durable rollback verification failed: {entry['path']}",
                    recovery_paths=(pending_path,),
                )
        _cleanup_created_directories(
            repo_root,
            payload,
            recovery_path=pending_path,
        )
        _cleanup_pending(state_root, state_fd, pending_fd, payload)
        return "rolled-back"
    except BaseException:
        try:
            os.close(pending_fd)
        except OSError:
            pass
        raise


class DurableBatchGuard:
    """A held repository-wide lock with crash recovery already completed."""

    def __init__(self, repo_root: Path, state_root: Path, state_fd: int) -> None:
        self.repo_root = repo_root
        self.state_root = state_root
        self.state_fd = state_fd

    def recover(self) -> str:
        return _recover_pending_locked(
            self.repo_root,
            self.state_root,
            self.state_fd,
        )

    def commit_batch(
        self,
        replacements: Mapping[Path, bytes | None],
        apply_batch: Callable[[], None],
        *,
        after_modes: Mapping[Path, int | None] | None = None,
    ) -> None:
        normalized: dict[Path, bytes | None] = {}
        for path, content in replacements.items():
            if content is not None and not isinstance(content, bytes):
                raise TypeError("durable replacement content must be bytes or None")
            candidate = (
                Path(path)
                if Path(path).is_absolute()
                else self.repo_root / Path(path)
            )
            absolute = self.repo_root / _relative_target(
                self.repo_root,
                candidate,
            )
            normalized[absolute] = content
        normalized_modes: dict[Path, int | None] = {}
        for path, mode in (after_modes or {}).items():
            candidate = (
                Path(path)
                if Path(path).is_absolute()
                else self.repo_root / Path(path)
            )
            absolute = self.repo_root / _relative_target(
                self.repo_root,
                candidate,
            )
            if absolute not in normalized:
                raise DurableBatchError(
                    f"after mode has no replacement target: {path}"
                )
            normalized_modes[absolute] = mode
        if not normalized:
            apply_batch()
            return
        _persist_journal(
            self.repo_root,
            self.state_root,
            self.state_fd,
            normalized,
            normalized_modes,
        )
        try:
            apply_batch()
        except BaseException as cause:
            try:
                self.recover()
            except BaseException as recovery_error:
                if hasattr(cause, "add_note"):
                    cause.add_note(
                        "durable batch recovery failed closed; pending journal "
                        f"preserved at {self.state_root / PENDING_DIRECTORY}: "
                        f"{recovery_error}"
                    )
                setattr(
                    cause,
                    "durable_recovery_paths",
                    (self.state_root / PENDING_DIRECTORY,),
                )
                raise cause from recovery_error
            raise
        outcome = self.recover()
        if outcome != "completed-after":
            raise DurableBatchRecoveryError(
                "durable batch callback returned without installing every "
                f"after state; recovery outcome={outcome}"
            )


@contextmanager
def durable_batch_lock_and_recover(
    repo_root: Path,
    *,
    timeout: float = 10.0,
) -> Iterator[DurableBatchGuard]:
    """Acquire the cross-tool batch lock and recover a prior hard exit."""
    if fcntl is None:  # pragma: no cover - repository CI is POSIX
        raise DurableBatchLockError("durable batch locks require POSIX flock")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout < 0
    ):
        raise ValueError("durable batch lock timeout must be finite and non-negative")
    root = _repository_root(Path(repo_root))
    state_root, state_fd = _ensure_state_root(root)
    lock_path = state_root / LOCK_FILENAME
    lock_fd = os.open(
        LOCK_FILENAME,
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=state_fd,
    )
    acquired = False
    try:
        _validate_lock_descriptor(state_fd, lock_fd, lock_path)
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise DurableBatchLockError(
                        f"durable batch is already active: {lock_path}"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(lock_fd)
        guard = DurableBatchGuard(root, state_root, state_fd)
        guard.recover()
        yield guard
    finally:
        try:
            if acquired:
                os.ftruncate(lock_fd, 0)
                os.fsync(lock_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            os.close(state_fd)


def commit_batch(
    repo_root: Path,
    replacements: Mapping[Path, bytes | None],
    apply_batch: Callable[[], None],
    *,
    after_modes: Mapping[Path, int | None] | None = None,
    timeout: float = 10.0,
) -> None:
    """Convenience wrapper for one locked, recoverable file batch."""
    with durable_batch_lock_and_recover(repo_root, timeout=timeout) as guard:
        guard.commit_batch(
            replacements,
            apply_batch,
            after_modes=after_modes,
        )


__all__ = [
    "DurableBatchError",
    "DurableBatchGuard",
    "DurableBatchLockError",
    "DurableBatchRecoveryError",
    "commit_batch",
    "durable_batch_lock_and_recover",
]

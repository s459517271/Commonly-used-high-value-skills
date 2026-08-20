#!/usr/bin/env python3
"""Register a new skill into the repository's provenance system.

This script handles the "last mile" of skill ingestion:
1. Validates the SKILL.md exists and has required frontmatter
2. Enriches frontmatter with missing fields (source, tags, dates)
3. Updates the provenance mapping (in-house.skills.json)
4. Optionally runs the full refresh pipeline

Usage:
    # Register a single skill
    python scripts/ingest_skill.py --dir skills/developer-engineering/vue-composition-api --source "github:vuejs/vue"

    # Register with explicit category and source URL
    python scripts/ingest_skill.py --dir skills/devops-sre/ansible-expert --source "skills.sh" --source-url "https://skills.sh/s/ansible"

    # Batch register all untracked skills
    python scripts/ingest_skill.py --batch

    # Dry run (show what would be done without writing)
    python scripts/ingest_skill.py --batch --dry-run
"""
from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from provenance_v2 import infer_channel, safe_relative_path
except ModuleNotFoundError:  # pragma: no cover - import path used by unit tests
    from scripts.provenance_v2 import infer_channel, safe_relative_path
try:
    from sync_upstream import (
        mapping_advisory_lock,
        validate_license_evidence,
    )
    from artifact_set_sync import skill_advisory_lock
    from durable_file_batch import (
        DurableBatchGuard,
        durable_batch_lock_and_recover,
    )
    from github_artifact_provider import (
        GitHubArtifactProvider,
        GitHubProviderError,
    )
except ModuleNotFoundError:  # pragma: no cover - package-style unit import
    from scripts.sync_upstream import (
        mapping_advisory_lock,
        validate_license_evidence,
    )
    from scripts.artifact_set_sync import skill_advisory_lock
    from scripts.durable_file_batch import (
        DurableBatchGuard,
        durable_batch_lock_and_recover,
    )
    from scripts.github_artifact_provider import (
        GitHubArtifactProvider,
        GitHubProviderError,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
PROVENANCE_FILE = REPO_ROOT / "docs" / "sources" / "in-house.skills.json"
EXTERNAL_PROVENANCE_FILE = (
    REPO_ROOT / "docs" / "sources" / "ingested-external.skills.json"
)
INHOUSE_SOURCES = {"in-house", "", "local-repo/in-house"}
PERMISSIVE_LICENSES = {
    "MIT", "Apache-2.0", "Apache 2.0", "BSD-2-Clause", "BSD-3-Clause",
    "ISC", "CC-BY-4.0", "CC0-1.0", "Unlicense", "0BSD", "MPL-2.0",
}
GITHUB_LICENSE_KEYS = {
    "mit": "MIT",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "isc": "ISC",
    "cc-by-4.0": "CC-BY-4.0",
    "cc0-1.0": "CC0-1.0",
    "unlicense": "Unlicense",
    "0bsd": "0BSD",
    "mpl-2.0": "MPL-2.0",
}
GITHUB_HOSTS = {"github.com", "www.github.com"}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract frontmatter key-value pairs from SKILL.md content."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def is_external_source(source: str) -> bool:
    return source.strip().strip('"').strip("'") not in INHOUSE_SOURCES


def normalize_license_tag(value: str) -> str:
    raw = value.strip().strip('"').strip("'")
    return GITHUB_LICENSE_KEYS.get(raw.lower(), raw)


def _normalize_github_repo(repo: str) -> str | None:
    value = repo.strip().removesuffix(".git")
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
        r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})",
        value,
    ):
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def github_repo_from_url(source_url: str) -> str | None:
    """Parse only an explicit HTTPS github.com repository URL."""
    if not source_url:
        return None
    try:
        parsed = urllib.parse.urlsplit(source_url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or hostname not in GITHUB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return _normalize_github_repo(f"{parts[0]}/{parts[1]}")


def github_repo_from_source(source: str, source_url: str = "") -> str | None:
    """Return a GitHub repository only when every declaration agrees."""
    normalized_source = source.strip().strip('"').strip("'")
    declared_repo: str | None = None
    if normalized_source.startswith("github:"):
        declared_repo = _normalize_github_repo(
            normalized_source.removeprefix("github:")
        )
        if declared_repo is None:
            return None
    url_repo = github_repo_from_url(source_url)
    if source_url and "github" in normalized_source.lower() and url_repo is None:
        return None
    if declared_repo and url_repo and declared_repo.lower() != url_repo.lower():
        return None
    return declared_repo or url_repo


def resolve_github_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def resolve_github_checkpoint(
    repo: str,
    ref: str,
    upstream_path: str,
) -> tuple[str | None, str | None]:
    """Resolve immutable repository and path commits using authenticated GitHub."""
    token = resolve_github_token()

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "skills-ingest-provenance",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def request_json(url: str) -> object:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        encoded_ref = urllib.parse.quote(ref, safe="")
        commit_payload = request_json(
            f"https://api.github.com/repos/{repo}/commits/{encoded_ref}"
        )
        resolved_commit = (
            commit_payload.get("sha")
            if isinstance(commit_payload, dict)
            else None
        )
        if (
            not isinstance(resolved_commit, str)
            or not COMMIT_RE.fullmatch(resolved_commit)
        ):
            return None, None
        query = urllib.parse.urlencode(
            {"path": upstream_path, "sha": resolved_commit, "per_page": 1}
        )
        path_payload = request_json(
            f"https://api.github.com/repos/{repo}/commits?{query}"
        )
        path_commit = (
            path_payload[0].get("sha")
            if isinstance(path_payload, list)
            and path_payload
            and isinstance(path_payload[0], dict)
            else None
        )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        TimeoutError,
    ):
        return None, None
    if not isinstance(path_commit, str) or not COMMIT_RE.fullmatch(path_commit):
        path_commit = None
    return resolved_commit, path_commit


def document_body(text: str) -> str:
    match = re.match(r"^---\s*\n.*?\n---(?:\s*\n|$)", text, re.DOTALL)
    body = text[match.end() :] if match else text
    return body.replace("\r\n", "\n").strip()


def regular_skill_files(skill_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(skill_dir.rglob("*")):
        if (
            path.is_symlink()
            or not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            continue
        files.append(path)
    return files


def classify_github_artifacts(
    *,
    skill_dir: Path,
    repo_root: Path,
    repo: str,
    resolved_commit: str,
    upstream_skill_path: str,
    artifact_maps: list[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Prove explicitly mapped upstream files and classify the rest as local."""
    try:
        from github_artifact_provider import (
            GitHubArtifactProvider,
            GitHubProviderError,
        )
    except ModuleNotFoundError:  # pragma: no cover - package-style test import
        from scripts.github_artifact_provider import (
            GitHubArtifactProvider,
            GitHubProviderError,
        )

    provider = GitHubArtifactProvider(resolve_github_token())
    try:
        tree = provider.tree(repo, resolved_commit)
    except GitHubProviderError as exc:
        raise ValueError(
            f"cannot inspect immutable upstream tree {repo}@{resolved_commit}: {exc}"
        ) from exc

    target_to_source: dict[str, str] = {}
    skill_root = skill_dir.relative_to(repo_root).as_posix()
    for source_path, target in artifact_maps or []:
        if not safe_relative_path(source_path) or not safe_relative_path(target):
            raise ValueError(
                f"artifact mapping must use safe relative paths: "
                f"{source_path!r}={target!r}"
            )
        if target in target_to_source:
            raise ValueError(f"duplicate artifact target: {target}")
        if target != skill_root and not target.startswith(f"{skill_root}/"):
            raise ValueError(
                f"artifact target is outside the skill directory: {target}"
            )
        target_to_source[target] = source_path

    external: list[dict[str, str]] = []
    local: list[dict[str, str]] = []
    used_targets: set[str] = set()
    for local_path in regular_skill_files(skill_dir):
        target = local_path.relative_to(repo_root).as_posix()
        if local_path.name == "SKILL.md":
            source_path = upstream_skill_path
            declared_source = target_to_source.get(target)
            if declared_source is not None:
                used_targets.add(target)
                if declared_source != source_path:
                    raise ValueError(
                        "canonical SKILL.md artifact mapping disagrees with "
                        f"--upstream-path: {declared_source} != {source_path}"
                    )
        else:
            source_path = target_to_source.get(target)
            if source_path is None:
                local.append({"source": target, "target": target, "type": "file"})
                continue
            used_targets.add(target)
        item = tree.get(source_path)
        upstream_bytes: bytes | None = None
        upstream_mode: str | None = None
        if isinstance(item, dict):
            item_type = item.get("type")
            upstream_mode = item.get("mode")
            if item_type == "blob" and upstream_mode not in {
                "100644",
                "100755",
            }:
                raise ValueError(
                    "declared upstream artifact is not a regular Git blob: "
                    f"{source_path} ({item_type}/{upstream_mode})"
                )
        if (
            isinstance(item, dict)
            and item.get("type") == "blob"
            and upstream_mode in {"100644", "100755"}
        ):
            local_metadata = local_path.lstat()
            if stat.S_ISLNK(local_metadata.st_mode) or not stat.S_ISREG(
                local_metadata.st_mode
            ):
                raise ValueError(
                    f"canonical artifact is not a regular file: {target}"
                )
            local_mode = _git_file_mode(local_metadata)
            if upstream_mode != local_mode:
                raise ValueError(
                    "declared upstream artifact mode does not match the "
                    f"canonical file: {source_path} -> {target} "
                    f"({upstream_mode} != {local_mode})"
                )
            blob_sha = item.get("sha")
            if not isinstance(blob_sha, str):
                raise ValueError(f"upstream artifact has no blob SHA: {source_path}")
            try:
                upstream_bytes = provider.blob(repo, blob_sha)
            except GitHubProviderError as exc:
                raise ValueError(
                    f"cannot read immutable upstream artifact {source_path}: {exc}"
                ) from exc

        if local_path.name == "SKILL.md":
            if upstream_bytes is None:
                raise ValueError(
                    f"declared upstream SKILL.md does not exist: {source_path}"
                )
            try:
                upstream_text = upstream_bytes.decode("utf-8")
                local_text = local_path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise ValueError("canonical external SKILL.md must be UTF-8") from exc
            if document_body(upstream_text) != document_body(local_text):
                raise ValueError(
                    "local SKILL.md body does not match the declared immutable "
                    "upstream; adapted canonical bodies must be ingested as an "
                    "original in-house skill instead"
                )
            external.append(
                {"source": source_path, "target": target, "type": "file"}
            )
        elif upstream_bytes is None:
            raise ValueError(
                f"explicitly mapped upstream artifact does not exist: {source_path}"
            )
        elif upstream_bytes == local_path.read_bytes():
            external.append(
                {"source": source_path, "target": target, "type": "file"}
            )
        else:
            raise ValueError(
                f"explicitly mapped artifact bytes differ from upstream: "
                f"{source_path} -> {target}"
            )
    unused = sorted(set(target_to_source) - used_targets)
    if unused:
        raise ValueError(
            "artifact mappings target missing files: " + ", ".join(unused)
        )
    return external, local


def resolve_external_license(
    fm: dict[str, str],
    source: str,
    source_url: str,
) -> tuple[str | None, str | None]:
    current = normalize_license_tag(fm.get("license", ""))
    if current and current not in PERMISSIVE_LICENSES:
        return None, f"license {current!r} is not in the permissive allowlist"

    source_value = source.strip().strip('"').strip("'")
    source_declares_github = source_value.startswith("github:")
    try:
        url_hostname = (
            urllib.parse.urlsplit(source_url).hostname or ""
        ).lower()
    except ValueError:
        url_hostname = ""
    url_looks_github = bool(source_url) and url_hostname in GITHUB_HOSTS
    repo = github_repo_from_source(source, source_url)
    if source_declares_github or url_looks_github:
        if repo is None:
            return None, "GitHub source and source_url are invalid or disagree"
        return (
            None,
            "GitHub license authorization requires an immutable resolved "
            "commit checkpoint",
        )

    if repo is not None:
        return None, "GitHub repository must be declared with a strict GitHub source"
    return (
        None,
        "non-GitHub license evidence cannot prove immutable content lineage; "
        "ingest an original in-house rewrite or declare its authoritative "
        "GitHub repository",
    )


def resolve_commit_bound_github_license(
    fm: dict[str, str],
    source: str,
    source_url: str,
    *,
    repo: str,
    resolved_commit: str,
) -> tuple[str | None, dict[str, str] | None, str | None]:
    """Verify complete license bytes at the exact immutable source commit."""
    declared = normalize_license_tag(fm.get("license", ""))
    if declared and declared not in PERMISSIVE_LICENSES:
        return (
            None,
            None,
            f"license {declared!r} is not in the permissive allowlist",
        )
    if github_repo_from_source(source, source_url) != repo:
        return (
            None,
            None,
            "GitHub source and source_url are invalid or disagree",
        )
    if not COMMIT_RE.fullmatch(resolved_commit):
        return (
            None,
            None,
            "GitHub license authorization requires a full immutable commit",
        )
    provider = GitHubArtifactProvider(resolve_github_token())
    try:
        evidence = provider.license_evidence(repo, resolved_commit.lower())
    except GitHubProviderError as exc:
        return (
            None,
            None,
            f"cannot verify immutable GitHub license: {exc}",
        )
    candidate = (
        evidence.api_spdx
        if evidence.api_spdx not in {None, "NOASSERTION"}
        else (
            evidence.spdx_candidates[0]
            if len(evidence.spdx_candidates) == 1
            else None
        )
    )
    checkpoint, error = validate_license_evidence(
        declared or candidate,
        evidence,
    )
    if error is not None or checkpoint is None:
        return None, None, error or "immutable license evidence is unavailable"
    return checkpoint["spdx"], checkpoint, None


def update_frontmatter_field(content: str, key: str, value: str) -> str:
    """Add or update a single frontmatter field."""
    m = re.match(r"^(---\s*\n)(.*?)(\n---)", content, re.DOTALL)
    if not m:
        return content
    header, fm_body, footer = m.group(1), m.group(2), m.group(3)

    # Check if key already exists
    pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.MULTILINE)
    if pattern.search(fm_body):
        # Update existing
        fm_body = pattern.sub(f"{key}: {value}", fm_body)
    else:
        # Add before closing ---
        fm_body = fm_body.rstrip() + f"\n{key}: {value}"

    return header + fm_body + footer + content[m.end():]


def get_tracked_skills(provenance_path: Path) -> set[str]:
    """Return every active claim from one mapping or a mappings directory."""
    mappings = (
        sorted(provenance_path.glob("*.skills.json"))
        if provenance_path.is_dir()
        else [provenance_path]
    )
    tracked: set[str] = set()
    for mapping in mappings:
        if not mapping.exists():
            continue
        data = json.loads(mapping.read_text(encoding="utf-8"))
        if data.get("schema_version") != 2:
            raise ValueError(f"active mapping must use provenance v2: {mapping}")
        entries = data.get("skills")
        if not isinstance(entries, list):
            raise ValueError(f"mapping skills must be a list: {mapping}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"mapping contains a non-object claim: {mapping}")
            name = entry.get("normalized_slug") or entry.get("video_name")
            if isinstance(name, str) and name:
                tracked.add(name)
    return tracked


def find_untracked_skills(skills_dir: Path, tracked: set[str]) -> list[Path]:
    """Find skills that exist on disk but are not in the provenance mapping."""
    untracked = []
    for skill_md in sorted(skills_dir.glob("*/*/SKILL.md")):
        skill_name = skill_md.parent.name
        if skill_name not in tracked:
            untracked.append(skill_md.parent)
    return untracked


def get_git_created_date(filepath: Path) -> str:
    """Get the first commit date for a file from git log."""
    try:
        result = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%aI", "--", str(filepath)],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=10
        )
        if result.stdout.strip():
            return result.stdout.strip().split("T")[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return date.today().isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def github_location(
    *,
    source: str,
    source_url: str,
    skill_name: str,
    upstream_path: str | None,
    upstream_ref: str,
) -> tuple[str, str, str] | None:
    """Resolve a GitHub repository, ref and SKILL path for v2 provenance."""
    source_value = source.strip().strip('"').strip("'")
    repo = github_repo_from_source(source, source_url)
    try:
        parsed = urllib.parse.urlsplit(source_url) if source_url else None
    except ValueError as exc:
        raise ValueError("source_url is invalid") from exc
    github_url = (
        parsed is not None
        and (parsed.hostname or "").lower() in GITHUB_HOSTS
    )
    if source_value.startswith("github:") or github_url:
        if repo is None:
            raise ValueError("GitHub source and source_url are invalid or disagree")
    elif repo is not None:
        raise ValueError("GitHub repository declaration is ambiguous")
    if not repo:
        return None

    resolved_ref = upstream_ref
    resolved_path = upstream_path
    if parsed is not None:
        parts = [
            urllib.parse.unquote(part)
            for part in parsed.path.split("/")
            if part
        ]
        if len(parts) >= 5 and parts[2] in {"blob", "tree"}:
            if upstream_ref == "main":
                resolved_ref = parts[3]
            if not resolved_path:
                resolved_path = "/".join(parts[4:]).rstrip("/")

    if not resolved_path:
        resolved_path = f"skills/{skill_name}/SKILL.md"
    elif not resolved_path.endswith("/SKILL.md") and resolved_path != "SKILL.md":
        resolved_path = f"{resolved_path.rstrip('/')}/SKILL.md"
    return repo, resolved_ref, resolved_path


def _mapping_payload(path: Path, source_url: str, today: str) -> dict:
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 2:
            raise ValueError(f"external provenance mapping must use schema v2: {path}")
        if not isinstance(payload.get("skills"), list):
            raise ValueError(f"external provenance mapping has invalid skills list: {path}")
        if not isinstance(payload.get("video"), dict):
            raise ValueError(f"external provenance mapping has invalid video: {path}")
        if not isinstance(payload.get("official_references"), list):
            raise ValueError(
                f"external provenance mapping has invalid official_references: "
                f"{path}"
            )
        if not isinstance(payload.get("verification_attempts"), list):
            raise ValueError(
                f"external provenance mapping has invalid verification_attempts: "
                f"{path}"
            )
        return payload
    return {
        "schema_version": 2,
        "video": {
            "url": source_url,
            "checked_at": today,
            "note": "External skills registered by scripts/ingest_skill.py.",
        },
        "official_references": [],
        "skills": [],
        "verification_attempts": [],
    }


def _assert_unclaimed(
    *,
    repo_root: Path,
    target_mapping: Path,
    repo_skill: str,
    skill_name: str,
) -> None:
    sources_dir = repo_root / "docs" / "sources"
    for mapping in sorted(sources_dir.glob("*.skills.json")):
        if mapping.resolve() == target_mapping.resolve():
            continue
        payload = json.loads(mapping.read_text(encoding="utf-8"))
        for entry in payload.get("skills", []):
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("repo_skill") == repo_skill
                or (
                    entry.get("normalized_slug") == skill_name
                    and entry.get("status") in {"verified_in_repo", "in_house"}
                )
            ):
                raise ValueError(
                    f"{skill_name} is already claimed by {mapping.relative_to(repo_root)}"
                )


def build_external_provenance_payload(
    *,
    skill_dir: Path,
    skill_content: str,
    source: str,
    source_url: str,
    license_value: str,
    mapping_path: Path,
    repo_root: Path,
    upstream_path: str | None = None,
    upstream_ref: str = "main",
    resolved_commit: str | None = None,
    path_commit: str | None = None,
    license_checkpoint: dict[str, str] | None = None,
    artifact_maps: list[tuple[str, str]] | None = None,
    existing_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fully validated v2 mapping without mutating the repository."""
    skill_md = skill_dir / "SKILL.md"
    frontmatter = parse_frontmatter(skill_content)
    skill_name = frontmatter.get("name") or skill_dir.name
    try:
        repo_skill = skill_md.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"skill directory is outside repository: {skill_dir}") from exc
    expected_repo_skill = f"skills/{skill_dir.parent.name}/{skill_name}/SKILL.md"
    if repo_skill != expected_repo_skill or not safe_relative_path(repo_skill):
        raise ValueError(
            "external skill must use canonical path "
            f"{expected_repo_skill!r}; got {repo_skill!r}"
        )

    location = github_location(
        source=source,
        source_url=source_url,
        skill_name=skill_name,
        upstream_path=upstream_path,
        upstream_ref=upstream_ref,
    )
    if location:
        repo, ref, source_path = location
        kind = "mirror"
        sync_mode = "monitor"
        discovered_commit, discovered_path_commit = resolve_github_checkpoint(
            repo,
            ref,
            source_path,
        )
        if discovered_commit is None or discovered_path_commit is None:
            raise ValueError(
                f"cannot resolve immutable GitHub checkpoint for "
                f"{repo}@{ref}:{source_path}"
            )
        if (
            resolved_commit is not None
            and resolved_commit.lower() != discovered_commit.lower()
        ):
            raise ValueError(
                "caller supplied --resolved-commit does not match the online "
                f"checkpoint: {resolved_commit} != {discovered_commit}"
            )
        if (
            path_commit is not None
            and path_commit.lower() != discovered_path_commit.lower()
        ):
            raise ValueError(
                "caller supplied --path-commit does not match the online path "
                f"checkpoint: {path_commit} != {discovered_path_commit}"
            )
        resolved_commit = discovered_commit
        path_commit = discovered_path_commit
    else:
        raise ValueError(
            "non-GitHub content lineage cannot own canonical artifacts; "
            "use an original in-house rewrite"
        )
    if not safe_relative_path(source_path):
        raise ValueError(f"upstream path must be a safe relative path: {source_path!r}")
    if (
        not isinstance(license_checkpoint, dict)
        or license_checkpoint.get("resolved_commit")
        != str(resolved_commit).lower()
        or license_checkpoint.get("spdx") != license_value
    ):
        raise ValueError(
            "immutable license checkpoint is missing or disagrees with the "
            "resolved source commit"
        )
    channel = infer_channel(ref, repo)

    today = date.today().isoformat()
    skill_bytes = skill_content.encode("utf-8")
    digest = sha256_bytes(skill_bytes)
    if location:
        external_artifacts, local_artifacts = classify_github_artifacts(
            skill_dir=skill_dir,
            repo_root=repo_root,
            repo=repo,
            resolved_commit=str(resolved_commit),
            upstream_skill_path=source_path,
            artifact_maps=artifact_maps,
        )
    else:
        external_artifacts = [
            {
                "source": source_path,
                "target": repo_skill,
                "type": "file",
            }
        ]
        external_targets = {repo_skill}
        local_artifacts = [
            {
                "source": path.relative_to(repo_root).as_posix(),
                "target": path.relative_to(repo_root).as_posix(),
                "type": "file",
            }
            for path in regular_skill_files(skill_dir)
            if path.relative_to(repo_root).as_posix() not in external_targets
        ]
    source_link = source_url or (
        f"https://github.com/{repo}" if location else f"https://{repo}"
    )
    origins = [
        {
            "repo": repo,
            "path": source_path,
            "license": license_value,
            "sync_mode": sync_mode,
            "artifacts": external_artifacts,
            "tracking": {
                "channel": channel,
                "ref": ref,
                "resolved_commit": resolved_commit,
                "path_commit": path_commit,
                "content_sha256": digest,
                "license_checkpoint": copy.deepcopy(license_checkpoint),
                "last_checked_at": today,
                "last_synced_at": today,
            },
        }
    ]
    if local_artifacts:
        origins.append(
            {
                "repo": "local-repo/curation",
                "path": skill_md.parent.relative_to(repo_root).as_posix(),
                "license": None,
                "sync_mode": "local-only",
                "artifacts": local_artifacts,
                "tracking": {
                    "channel": "local",
                    "ref": "local",
                    "resolved_commit": None,
                    "path_commit": None,
                    "content_sha256": None,
                    "last_checked_at": today,
                    "last_synced_at": today,
                },
            }
        )
        if kind != "snapshot":
            kind = "overlay"
            sync_mode = "monitor"
    managed_paths = regular_skill_files(skill_dir)
    _assert_tracked_modes_clean(repo_root, managed_paths)
    entry = {
        "video_name": skill_name,
        "normalized_slug": skill_name,
        "status": "verified_in_repo",
        "repo_skill": repo_skill,
        "source": source_link,
        "notes": "External skill registered by scripts/ingest_skill.py.",
        "upstream": {
            "repo": repo,
            "path": source_path,
            "ref": ref,
            "last_checked_at": today,
            "last_synced_at": today,
            "last_synced_commit": resolved_commit,
            "sync_mode": sync_mode,
        },
        "kind": kind,
        "sync_mode": sync_mode,
        "origins": origins,
        "managed_files": [
            {
                "path": path.relative_to(repo_root).as_posix(),
                "owner": skill_name,
                "sha256": (
                    digest
                    if path == skill_md
                    else sha256_file(path)
                ),
                "mode": _git_file_mode(path.lstat()),
            }
            for path in managed_paths
        ],
    }

    _assert_unclaimed(
        repo_root=repo_root,
        target_mapping=mapping_path,
        repo_skill=repo_skill,
        skill_name=skill_name,
    )
    payload = (
        copy.deepcopy(existing_payload)
        if existing_payload is not None
        else _mapping_payload(mapping_path, source_link, today)
    )
    payload["video"]["checked_at"] = today
    entries = [
        existing
        for existing in payload["skills"]
        if isinstance(existing, dict)
        and existing.get("repo_skill") != repo_skill
        and existing.get("normalized_slug") != skill_name
    ]
    entries.append(entry)
    entries.sort(key=lambda item: str(item.get("normalized_slug") or ""))
    payload["skills"] = entries
    payload.setdefault("official_references", [])
    if source_link and not any(
        isinstance(reference, dict) and reference.get("url") == source_link
        for reference in payload["official_references"]
    ):
        payload["official_references"].append(
            {
                "name": f"{skill_name} upstream",
                "url": source_link,
                "purpose": "Original external source registered during ingestion.",
            }
        )
    attempt = {
        "date": today,
        "method": "ingest-skill",
        "target": repo_skill,
        "result": "success",
        "evidence": f"Registered external provenance for {skill_name}",
    }
    attempts = payload.setdefault("verification_attempts", [])
    if attempt not in attempts:
        attempts.append(attempt)
    return payload


def register_external_provenance(
    *,
    skill_dir: Path,
    source: str,
    source_url: str,
    license_value: str,
    mapping_path: Path,
    repo_root: Path,
    upstream_path: str | None = None,
    upstream_ref: str = "main",
    resolved_commit: str | None = None,
    path_commit: str | None = None,
    artifact_maps: list[tuple[str, str]] | None = None,
) -> None:
    """Compatibility wrapper for callers that already enriched SKILL.md."""
    with acquire_ingest_locks(
        repo_root=repo_root,
        skill_dirs=[skill_dir],
        mapping_paths=[mapping_path],
    ) as durable_guard:
        plan = prepare_ingest(
            skill_dir,
            source,
            source_url,
            external_mapping=mapping_path,
            repo_root=repo_root,
            upstream_path=upstream_path,
            upstream_ref=upstream_ref,
            resolved_commit=resolved_commit,
            path_commit=path_commit,
            artifact_maps=artifact_maps,
        )
        planned_license = normalize_license_tag(
            parse_frontmatter(
                plan.after_skill.decode("utf-8")
            ).get("license", "")
        )
        if normalize_license_tag(license_value) != planned_license:
            raise ValueError(
                "caller license disagrees with verified upstream license"
            )
        commit_ingest_plans(
            [plan],
            locks_held=True,
            durable_guard=durable_guard,
        )


class IngestPlan:
    def __init__(
        self,
        *,
        skill_name: str,
        skill_md: Path,
        before_skill: bytes | None,
        after_skill: bytes,
        after_mode: int | None = None,
        mapping_path: Path | None = None,
        before_mapping: bytes | None = None,
        after_mapping: bytes | None = None,
        mapping_payload: dict[str, Any] | None = None,
        repo_root: Path | None = None,
        input_fingerprint: str | None = None,
        before_checkpoint: dict[str, Any] | None = None,
        mapping_checkpoint: dict[str, Any] | None = None,
        before_parent_checkpoint: list[dict[str, Any]] | None = None,
        mapping_parent_checkpoint: list[dict[str, Any]] | None = None,
    ) -> None:
        self.skill_name = skill_name
        self.skill_md = skill_md
        self.before_skill = before_skill
        self.after_skill = after_skill
        self.after_mode = after_mode
        self.mapping_path = mapping_path
        self.before_mapping = before_mapping
        self.after_mapping = after_mapping
        self.mapping_payload = mapping_payload
        self.repo_root = repo_root
        self.input_fingerprint = input_fingerprint
        self.before_checkpoint = before_checkpoint
        self.mapping_checkpoint = mapping_checkpoint
        self.before_parent_checkpoint = before_parent_checkpoint
        self.mapping_parent_checkpoint = mapping_parent_checkpoint


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory by walking every ancestor without symlinks."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", _directory_open_flags())
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"not a directory: {path}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def open_parent_from_repo_nofollow(
    repo_root: Path,
    target: Path,
) -> tuple[int, str]:
    root_absolute = Path(os.path.abspath(repo_root))
    target_absolute = Path(os.path.abspath(target))
    try:
        relative = target_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError(f"target escapes repository root: {target}") from exc
    if not relative.parts or any(
        component in {"", ".", ".."} for component in relative.parts
    ):
        raise ValueError(f"unsafe repository target: {target}")
    descriptor = open_directory_nofollow(root_absolute)
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def capture_parent_checkpoint(
    repo_root: Path,
    target: Path,
) -> list[dict[str, Any]]:
    """Capture every target-parent directory without following symlinks."""
    root_absolute = Path(os.path.abspath(repo_root))
    target_absolute = Path(os.path.abspath(target))
    try:
        relative = target_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError(f"target escapes repository root: {target}") from exc
    if not relative.parts or any(
        component in {"", ".", ".."} for component in relative.parts
    ):
        raise ValueError(f"unsafe repository target: {target}")
    parent_parts = relative.parts[:-1]
    checkpoint: list[dict[str, Any]] = []
    descriptor = open_directory_nofollow(root_absolute)
    missing = False
    try:
        traversed: list[str] = []
        for component in parent_parts:
            traversed.append(component)
            relative_directory = PurePosixPath(*traversed).as_posix()
            if missing:
                checkpoint.append(
                    {
                        "path": relative_directory,
                        "exists": False,
                        "dev": None,
                        "ino": None,
                        "mode": None,
                    }
                )
                continue
            try:
                metadata = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                missing = True
                checkpoint.append(
                    {
                        "path": relative_directory,
                        "exists": False,
                        "dev": None,
                        "ino": None,
                        "mode": None,
                    }
                )
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise ValueError(
                    f"target parent is not a regular directory: "
                    f"{root_absolute / relative_directory}"
                )
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            opened = os.fstat(next_descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                os.close(next_descriptor)
                raise RuntimeError(
                    f"target parent changed while reading: {target}"
                )
            os.close(descriptor)
            descriptor = next_descriptor
            checkpoint.append(
                {
                    "path": relative_directory,
                    "exists": True,
                    "dev": opened.st_dev,
                    "ino": opened.st_ino,
                    "mode": stat.S_IMODE(opened.st_mode),
                }
            )
    finally:
        os.close(descriptor)
    return checkpoint


def assert_parent_checkpoint(
    repo_root: Path,
    target: Path,
    expected: list[dict[str, Any]],
) -> None:
    if capture_parent_checkpoint(repo_root, target) != expected:
        raise RuntimeError(
            f"transaction target parent changed after staging: {target}"
        )


def _checkpoint_from_stat(
    path: Path,
    metadata: os.stat_result | None,
    content: bytes | None,
) -> dict[str, Any]:
    if metadata is None:
        return {
            "exists": False,
            "dev": None,
            "ino": None,
            "mode": None,
            "size": None,
            "sha256": None,
        }
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"transaction target must be a regular file: {path}")
    if content is None:
        content = path.read_bytes()
    return {
        "exists": True,
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": metadata.st_size,
        "sha256": sha256_bytes(content),
    }


def capture_target_checkpoint(
    path: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    if repo_root is None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return _checkpoint_from_stat(path, None, None)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"transaction target must not be a symlink: {path}")
        return _checkpoint_from_stat(path, metadata, path.read_bytes())

    parent_checkpoint = capture_parent_checkpoint(repo_root, path)
    if any(not item["exists"] for item in parent_checkpoint):
        return _checkpoint_from_stat(path, None, None)
    parent_fd, leaf = open_parent_from_repo_nofollow(repo_root, path)
    try:
        return _capture_target_at(parent_fd, leaf, path)
    finally:
        os.close(parent_fd)


def _capture_target_at(
    parent_fd: int,
    leaf: str,
    path: Path,
) -> dict[str, Any]:
    try:
        metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _checkpoint_from_stat(path, None, None)
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"transaction target must not be a symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(leaf, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(file_descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise RuntimeError(f"target inode changed while reading: {path}")
        with os.fdopen(os.dup(file_descriptor), "rb") as stream:
            content = stream.read()
        completed = os.fstat(file_descriptor)
        if (
            completed.st_dev != opened.st_dev
            or completed.st_ino != opened.st_ino
            or completed.st_mode != opened.st_mode
            or completed.st_size != opened.st_size
            or completed.st_mtime_ns != opened.st_mtime_ns
        ):
            raise RuntimeError(f"target changed while reading: {path}")
    finally:
        os.close(file_descriptor)
    return _checkpoint_from_stat(path, completed, content)


def assert_target_checkpoint(
    path: Path,
    expected: dict[str, Any],
    *,
    repo_root: Path | None = None,
) -> None:
    current = capture_target_checkpoint(path, repo_root=repo_root)
    if current != expected:
        raise RuntimeError(
            f"transaction target changed after staging: {path}"
        )


def _path_state(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"type": "missing", "sha256": ""}
    if stat.S_ISLNK(metadata.st_mode):
        return {"type": "symlink", "sha256": os.readlink(path)}
    if not stat.S_ISREG(metadata.st_mode):
        return {"type": "other", "sha256": str(metadata.st_mode)}
    return {
        "type": "file",
        "sha256": sha256_file(path),
        "mode": _git_file_mode(metadata),
    }


def _git_file_mode(metadata: os.stat_result) -> str:
    """Normalize a regular filesystem mode to the two Git blob modes."""
    return "100755" if stat.S_IMODE(metadata.st_mode) & 0o111 else "100644"


def _assert_tracked_modes_clean(repo_root: Path, paths: list[Path]) -> None:
    """Reject chmod drift from the Git index before it becomes ownership state.

    New, untracked skill files have no index authority yet and are allowed.
    Existing tracked files must retain the executable bit recorded by Git.
    Nested test repositories are deliberately not compared against an outer
    worktree's index.
    """
    if not paths:
        return
    try:
        top_level = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot inspect Git index modes: {exc}") from exc
    if top_level.returncode != 0:
        return
    try:
        git_root = Path(top_level.stdout.strip()).resolve(strict=True)
        resolved_repo = repo_root.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve Git worktree for mode audit: {exc}") from exc
    if git_root != resolved_repo:
        return

    relative_paths: list[str] = []
    by_relative: dict[str, Path] = {}
    for path in paths:
        try:
            relative = path.resolve(strict=True).relative_to(resolved_repo).as_posix()
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise ValueError(f"managed skill file escapes repository: {path}") from exc
        relative_paths.append(relative)
        by_relative[relative] = path
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(resolved_repo),
                "ls-files",
                "--stage",
                "-z",
                "--",
                *relative_paths,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot read Git index modes: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            "cannot read Git index modes"
            + (f": {detail}" if detail else "")
        )

    index_modes: dict[str, str] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ValueError("Git index returned a malformed stage record")
        try:
            mode = fields[0].decode("ascii")
            relative = raw_path.decode("utf-8", errors="surrogateescape")
        except UnicodeDecodeError as exc:
            raise ValueError("Git index returned an invalid mode") from exc
        if fields[2] != b"0":
            raise ValueError(
                f"Git index contains an unmerged managed file: {relative}"
            )
        if mode not in {"100644", "100755"}:
            raise ValueError(
                f"Git index managed file is not a regular blob: {relative}: {mode}"
            )
        index_modes[relative] = mode

    dirty: list[str] = []
    for relative, expected in index_modes.items():
        path = by_relative.get(relative)
        if path is None:
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"managed skill file is not regular: {relative}")
        actual = _git_file_mode(metadata)
        if actual != expected:
            dirty.append(f"{relative} ({expected} -> {actual})")
    if dirty:
        raise ValueError(
            "refusing to authorize dirty executable-mode changes: "
            + ", ".join(sorted(dirty))
        )


def capture_ingest_fingerprint(
    *,
    skill_dir: Path,
    mapping_path: Path | None,
    repo_root: Path,
) -> str:
    """Hash the entire skill tree and every provenance claim input."""
    inventory: dict[str, dict[str, str]] = {}
    repo_absolute = Path(os.path.abspath(repo_root))
    skill_absolute = Path(os.path.abspath(skill_dir))
    try:
        skill_relative = skill_absolute.relative_to(repo_absolute).as_posix()
    except ValueError as exc:
        raise ValueError("skill directory escapes repository root") from exc
    repository_files = _walk_repository_regular_files(repo_absolute)
    skill_prefix = skill_relative.rstrip("/") + "/"
    for relative, (content, mode) in repository_files.items():
        is_skill_input = (
            relative == f"{skill_relative}/SKILL.md"
            or relative.startswith(skill_prefix)
        )
        is_claim = (
            relative.startswith("docs/sources/")
            and "/" not in relative.removeprefix("docs/sources/")
            and relative.endswith(".skills.json")
        )
        if is_skill_input or is_claim:
            inventory[relative] = {
                "type": "file",
                "sha256": sha256_bytes(content),
                "mode": str(mode),
            }
    if mapping_path is not None:
        mapping_absolute = Path(os.path.abspath(mapping_path))
        try:
            mapping_relative = mapping_absolute.relative_to(
                repo_absolute
            ).as_posix()
        except ValueError as exc:
            raise ValueError("mapping path escapes repository root") from exc
        inventory.setdefault(
            mapping_relative,
            _path_state(mapping_absolute),
        )
    encoded = json.dumps(
        inventory,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextlib.contextmanager
def _acquire_ingest_locks_after_recovery(
    *,
    repo_root: Path,
    skill_dirs: list[Path],
    mapping_paths: list[Path],
    timeout: float = 10.0,
    recover_pending: bool = True,
):
    """Acquire the exact sync mapping/skill locks in one global order."""
    repo_absolute = Path(os.path.abspath(repo_root))
    root_fd = open_directory_nofollow(repo_absolute)
    os.close(root_fd)
    sources_dir = repo_absolute / "docs" / "sources"
    sources_fd = open_directory_nofollow(sources_dir)
    try:
        with os.scandir(sources_fd) as entries:
            mapping_names = sorted(
                entry.name
                for entry in entries
                if entry.name.endswith(".skills.json")
            )
    finally:
        os.close(sources_fd)
    all_mappings = {
        sources_dir / name for name in mapping_names
    }
    all_mappings.update(Path(path) for path in mapping_paths)
    normalized_mappings: list[Path] = []
    for path in all_mappings:
        absolute = Path(os.path.abspath(path))
        parent_fd, leaf = open_parent_from_repo_nofollow(
            repo_absolute,
            absolute,
        )
        try:
            try:
                metadata = os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                metadata = None
            if metadata is not None and (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise ValueError(f"unsafe mapping lock target: {absolute}")
        finally:
            os.close(parent_fd)
        normalized_mappings.append(absolute)

    skill_roots: dict[str, Path] = {}
    for skill_dir in skill_dirs:
        absolute = Path(os.path.abspath(skill_dir))
        try:
            relative = absolute.relative_to(repo_absolute).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"skill lock target escapes repository: {skill_dir}"
            ) from exc
        if not safe_relative_path(relative):
            raise ValueError(f"unsafe skill lock identity: {relative}")
        # A crashed artifact transaction may have moved the old canonical
        # directory aside. Validate the complete parent chain without
        # requiring the final directory to exist; the engine's public lock
        # performs journal recovery after acquisition.
        parent_fd, leaf = open_parent_from_repo_nofollow(
            repo_absolute,
            absolute,
        )
        try:
            try:
                metadata = os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                metadata = None
            if metadata is not None and (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise ValueError(f"unsafe skill lock target: {absolute}")
        finally:
            os.close(parent_fd)
        skill_roots[relative] = absolute

    claims_identity = sources_dir / ".claims.lock-identity"
    with contextlib.ExitStack() as locks:
        # Global order: claims -> every mapping (sorted) -> every skill (sorted).
        locks.enter_context(
            mapping_advisory_lock(claims_identity, timeout=timeout)
        )
        for mapping in sorted(set(normalized_mappings), key=str):
            locks.enter_context(
                mapping_advisory_lock(mapping, timeout=timeout)
            )
        for skill_root in sorted(skill_roots):
            locks.enter_context(
                skill_advisory_lock(
                    repo_absolute,
                    skill_root,
                    timeout=timeout,
                    recover_pending=recover_pending,
                )
            )
            # Recovery is complete when the public lock context is entered.
            # Re-open every ancestor and the recovered canonical directory
            # before any caller fingerprints or writes it.
            recovered_fd = open_directory_nofollow(
                skill_roots[skill_root]
            )
            os.close(recovered_fd)
        yield


@contextlib.contextmanager
def acquire_ingest_locks(
    *,
    repo_root: Path,
    skill_dirs: list[Path],
    mapping_paths: list[Path],
    timeout: float = 10.0,
    durable: bool = True,
):
    """Acquire durable-global, claims, mappings, then skills in fixed order."""
    if not durable:
        with _acquire_ingest_locks_after_recovery(
            repo_root=repo_root,
            skill_dirs=skill_dirs,
            mapping_paths=mapping_paths,
            timeout=timeout,
            recover_pending=False,
        ):
            yield None
        return
    with durable_batch_lock_and_recover(
        repo_root,
        timeout=timeout,
    ) as durable_guard:
        with _acquire_ingest_locks_after_recovery(
            repo_root=repo_root,
            skill_dirs=skill_dirs,
            mapping_paths=mapping_paths,
            timeout=timeout,
            recover_pending=True,
        ):
            yield durable_guard


def parse_artifact_map(value: str) -> tuple[str, str]:
    source, separator, target = value.partition("=")
    if not separator or not safe_relative_path(source) or not safe_relative_path(target):
        raise ValueError(
            "artifact map must be a safe repository-relative source=target pair"
        )
    return source, target


def load_artifact_manifest(path: Path) -> list[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items: object = payload.get("artifacts") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise ValueError("artifact manifest must be a list or contain artifacts[]")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"artifact manifest item {index} must be an object")
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError(
                f"artifact manifest item {index} requires source and target"
            )
        result.append(parse_artifact_map(f"{source}={target}"))
    return result


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def prepare_ingest(
    skill_dir: Path,
    source: str,
    source_url: str,
    *,
    external_mapping: Path | None = None,
    repo_root: Path = REPO_ROOT,
    upstream_path: str | None = None,
    upstream_ref: str = "main",
    resolved_commit: str | None = None,
    path_commit: str | None = None,
    artifact_maps: list[tuple[str, str]] | None = None,
    existing_mapping_payload: dict[str, Any] | None = None,
) -> IngestPlan:
    """Run every filesystem/network preflight and return an immutable plan."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file() or skill_md.is_symlink():
        raise ValueError(f"{skill_md} must be a regular file")
    _assert_tracked_modes_clean(repo_root, regular_skill_files(skill_dir))
    fingerprint_mapping = external_mapping or (
        repo_root / "docs" / "sources" / "ingested-external.skills.json"
    )
    initial_fingerprint = capture_ingest_fingerprint(
        skill_dir=skill_dir,
        mapping_path=fingerprint_mapping,
        repo_root=repo_root,
    )

    before_skill = skill_md.read_bytes()
    try:
        content = before_skill.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{skill_md} must be UTF-8") from exc
    fm = parse_frontmatter(content)

    if not fm.get("name"):
        raise ValueError(f"{skill_dir.name} — missing 'name' in frontmatter")

    skill_name = fm["name"]
    if skill_name != skill_dir.name:
        raise ValueError(
            f"frontmatter name {skill_name!r} must match directory "
            f"{skill_dir.name!r}"
        )
    today = date.today().isoformat()
    declared_source = fm.get("source", "")
    if (
        declared_source
        and declared_source not in INHOUSE_SOURCES
        and source not in INHOUSE_SOURCES
        and declared_source != source
    ):
        raise ValueError(
            f"frontmatter source {declared_source!r} disagrees with "
            f"caller source {source!r}"
        )
    effective_source = (
        declared_source
        if declared_source and declared_source not in INHOUSE_SOURCES
        else source
    )
    effective_source_url = fm.get("source_url") or source_url
    if fm.get("source_url") and source_url and fm["source_url"] != source_url:
        raise ValueError("frontmatter source_url disagrees with caller source_url")

    detected_license: str | None = None
    immutable_license_checkpoint: dict[str, str] | None = None
    if is_external_source(effective_source):
        location = github_location(
            source=effective_source,
            source_url=effective_source_url,
            skill_name=skill_name,
            upstream_path=upstream_path,
            upstream_ref=upstream_ref,
        )
        if location is None:
            _detected, license_error = resolve_external_license(
                fm,
                effective_source,
                effective_source_url,
            )
            raise ValueError(
                f"{skill_name} — {license_error or 'external source is unsupported'}"
            )
        repo, resolved_ref, resolved_path = location
        if not safe_relative_path(resolved_path):
            raise ValueError(
                f"upstream path must be a safe relative path: {resolved_path!r}"
            )
        discovered_commit, discovered_path_commit = (
            resolve_github_checkpoint(
                repo,
                resolved_ref,
                resolved_path,
            )
        )
        if discovered_commit is None or discovered_path_commit is None:
            raise ValueError(
                f"cannot resolve immutable GitHub checkpoint for "
                f"{repo}@{resolved_ref}:{resolved_path}"
            )
        if (
            resolved_commit is not None
            and resolved_commit.lower() != discovered_commit.lower()
        ):
            raise ValueError(
                "caller supplied --resolved-commit does not match the online "
                f"checkpoint: {resolved_commit} != {discovered_commit}"
            )
        if (
            path_commit is not None
            and path_commit.lower() != discovered_path_commit.lower()
        ):
            raise ValueError(
                "caller supplied --path-commit does not match the online path "
                f"checkpoint: {path_commit} != {discovered_path_commit}"
            )
        resolved_commit = discovered_commit
        path_commit = discovered_path_commit
        (
            detected_license,
            immutable_license_checkpoint,
            license_error,
        ) = resolve_commit_bound_github_license(
            fm,
            effective_source,
            effective_source_url,
            repo=repo,
            resolved_commit=discovered_commit,
        )
        if license_error:
            raise ValueError(f"{skill_name} — {license_error}")

    # Enrich frontmatter with source info
    updated = content
    if not fm.get("source") or fm.get("source") == "in-house":
        if source:
            updated = update_frontmatter_field(updated, "source", f'"{source}"')
    if not fm.get("source_url") and source_url:
        updated = update_frontmatter_field(updated, "source_url", f'"{source_url}"')
    if detected_license and not fm.get("license"):
        updated = update_frontmatter_field(updated, "license", detected_license)
    if not fm.get("created_at"):
        created = get_git_created_date(skill_md)
        updated = update_frontmatter_field(updated, "created_at", f'"{created}"')
    if not fm.get("updated_at"):
        updated = update_frontmatter_field(updated, "updated_at", f'"{today}"')

    mapping_path: Path | None = None
    mapping_before: bytes | None = None
    mapping_after: bytes | None = None
    mapping_payload: dict[str, Any] | None = None
    if is_external_source(effective_source):
        mapping_path = external_mapping or (
            repo_root / "docs" / "sources" / "ingested-external.skills.json"
        )
        try:
            mapping_path.resolve().relative_to(
                (repo_root / "docs" / "sources").resolve()
            )
        except ValueError as exc:
            raise ValueError(
                "external mapping must stay under docs/sources"
            ) from exc
        if not mapping_path.name.endswith(".skills.json"):
            raise ValueError("external mapping name must end with .skills.json")
        if mapping_path.is_symlink() or (
            mapping_path.exists() and not mapping_path.is_file()
        ):
            raise ValueError(
                f"external mapping must be a regular non-symlink file: "
                f"{mapping_path}"
            )
        mapping_before = (
            mapping_path.read_bytes() if mapping_path.exists() else None
        )
        mapping_payload = build_external_provenance_payload(
            skill_dir=skill_dir,
            skill_content=updated,
            source=effective_source,
            source_url=effective_source_url,
            license_value=detected_license or normalize_license_tag(fm.get("license", "")),
            mapping_path=mapping_path,
            repo_root=repo_root,
            upstream_path=upstream_path,
            upstream_ref=upstream_ref,
            resolved_commit=resolved_commit,
            path_commit=path_commit,
            license_checkpoint=immutable_license_checkpoint,
            artifact_maps=artifact_maps,
            existing_payload=existing_mapping_payload,
        )
        mapping_after = _json_bytes(mapping_payload)

    final_fingerprint = capture_ingest_fingerprint(
        skill_dir=skill_dir,
        mapping_path=fingerprint_mapping,
        repo_root=repo_root,
    )
    if final_fingerprint != initial_fingerprint:
        raise ValueError(
            "ingest inputs changed during preflight; retry under a stable "
            "claim/mapping/skill snapshot"
        )
    return IngestPlan(
        skill_name=skill_name,
        skill_md=skill_md,
        before_skill=before_skill,
        after_skill=updated.encode("utf-8"),
        after_mode=stat.S_IMODE(skill_md.lstat().st_mode),
        mapping_path=mapping_path,
        before_mapping=mapping_before,
        after_mapping=mapping_after,
        mapping_payload=mapping_payload,
        repo_root=repo_root,
        input_fingerprint=final_fingerprint,
        before_checkpoint=capture_target_checkpoint(
            skill_md,
            repo_root=repo_root,
        ),
        mapping_checkpoint=(
            capture_target_checkpoint(mapping_path, repo_root=repo_root)
            if mapping_path is not None
            else None
        ),
        before_parent_checkpoint=capture_parent_checkpoint(
            repo_root,
            skill_md,
        ),
        mapping_parent_checkpoint=(
            capture_parent_checkpoint(repo_root, mapping_path)
            if mapping_path is not None
            else None
        ),
    )


def _write_atomic_bytes(
    path: Path,
    content: bytes,
    *,
    repo_root: Path | None = None,
    expected_checkpoint: dict[str, Any] | None = None,
    expected_parent_checkpoint: list[dict[str, Any]] | None = None,
    desired_mode: int | None = None,
) -> None:
    if desired_mode is not None and (
        isinstance(desired_mode, bool)
        or not isinstance(desired_mode, int)
        or desired_mode < 0
        or desired_mode > 0o777
    ):
        raise ValueError(f"invalid desired file mode for {path}: {desired_mode!r}")
    if repo_root is not None and expected_checkpoint is not None:
        _write_atomic_bytes_secure(
            path,
            content,
            repo_root=repo_root,
            expected_checkpoint=expected_checkpoint,
            expected_parent_checkpoint=expected_parent_checkpoint,
            desired_mode=desired_mode,
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    original_mode: int | None = None
    try:
        destination_stat = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(destination_stat.st_mode):
            raise ValueError(f"refusing to replace symlink destination: {path}")
        if not stat.S_ISREG(destination_stat.st_mode):
            raise ValueError(
                f"refusing to replace non-regular destination: {path}"
            )
        original_mode = stat.S_IMODE(destination_stat.st_mode)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temporary)
    try:
        created_stat = os.fstat(fd)
        if not stat.S_ISREG(created_stat.st_mode):
            raise RuntimeError(f"temporary path is not regular: {temp_path}")
        installed_mode = (
            desired_mode
            if desired_mode is not None
            else original_mode
            if original_mode is not None
            else 0o644
        )
        os.fchmod(fd, installed_mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        named_stat = temp_path.lstat()
        if (
            not stat.S_ISREG(named_stat.st_mode)
            or named_stat.st_dev != created_stat.st_dev
            or named_stat.st_ino != created_stat.st_ino
        ):
            raise RuntimeError(f"temporary path changed before replace: {temp_path}")
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        installed = path.lstat()
        if (
            not stat.S_ISREG(installed.st_mode)
            or stat.S_IMODE(installed.st_mode) != installed_mode
            or path.read_bytes() != content
        ):
            raise RuntimeError(
                f"installed target failed byte/mode verification: {path}"
            )
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def _write_atomic_bytes_secure(
    path: Path,
    content: bytes,
    *,
    repo_root: Path,
    expected_checkpoint: dict[str, Any],
    expected_parent_checkpoint: list[dict[str, Any]] | None = None,
    desired_mode: int | None = None,
) -> None:
    """Replace one target through stable dirfds after checkpoint revalidation."""
    if expected_parent_checkpoint is not None:
        assert_parent_checkpoint(
            repo_root,
            path,
            expected_parent_checkpoint,
        )
    assert_target_checkpoint(
        path,
        expected_checkpoint,
        repo_root=repo_root,
    )
    parent_fd, leaf = open_parent_from_repo_nofollow(repo_root, path)
    temp_name = f".{leaf}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        parent_identity = os.fstat(parent_fd)
        if expected_parent_checkpoint:
            expected_parent = expected_parent_checkpoint[-1]
            if (
                not expected_parent["exists"]
                or parent_identity.st_dev != expected_parent["dev"]
                or parent_identity.st_ino != expected_parent["ino"]
            ):
                raise RuntimeError(
                    f"transaction target parent inode changed: {path}"
                )
        if _capture_target_at(parent_fd, leaf, path) != expected_checkpoint:
            raise RuntimeError(
                f"transaction target changed before writer prepare: {path}"
            )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        temporary_created = True
        try:
            created_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(created_metadata.st_mode):
                raise RuntimeError(
                    f"transaction temporary is not regular: {path}"
                )
            mode = (
                desired_mode
                if desired_mode is not None
                else int(expected_checkpoint["mode"])
                if expected_checkpoint.get("exists")
                else 0o644
            )
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        temporary_metadata = os.stat(
            temp_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(temporary_metadata.st_mode):
            raise RuntimeError(f"transaction temporary is not regular: {path}")
        if (
            temporary_metadata.st_dev != created_metadata.st_dev
            or temporary_metadata.st_ino != created_metadata.st_ino
        ):
            raise RuntimeError(
                f"transaction temporary inode changed before replace: {path}"
            )
        # The destination checkpoint (including inode) is checked again at the
        # exact replace boundary.
        if expected_parent_checkpoint is not None:
            assert_parent_checkpoint(
                repo_root,
                path,
                expected_parent_checkpoint,
            )
        if _capture_target_at(parent_fd, leaf, path) != expected_checkpoint:
            raise RuntimeError(
                f"transaction target changed immediately before replace: {path}"
            )
        os.replace(
            temp_name,
            leaf,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_created = False
        os.fsync(parent_fd)

        verification_fd, verification_leaf = open_parent_from_repo_nofollow(
            repo_root,
            path,
        )
        try:
            reopened_identity = os.fstat(verification_fd)
            if (
                reopened_identity.st_dev != parent_identity.st_dev
                or reopened_identity.st_ino != parent_identity.st_ino
            ):
                raise RuntimeError(
                    f"target parent changed across replace: {path}"
                )
            installed = _capture_target_at(
                verification_fd,
                verification_leaf,
                path,
            )
            if (
                not installed["exists"]
                or installed["size"] != len(content)
                or installed["sha256"] != sha256_bytes(content)
                or installed["mode"] != mode
            ):
                raise RuntimeError(
                    f"installed target failed byte verification: {path}"
                )
        finally:
            os.close(verification_fd)
    finally:
        if temporary_created:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _unlink_target_secure(
    path: Path,
    *,
    repo_root: Path | None,
    expected_checkpoint: dict[str, Any],
    expected_parent_checkpoint: list[dict[str, Any]] | None = None,
) -> None:
    if repo_root is None:
        assert_target_checkpoint(path, expected_checkpoint)
        path.unlink(missing_ok=True)
        return
    if expected_parent_checkpoint is not None:
        assert_parent_checkpoint(
            repo_root,
            path,
            expected_parent_checkpoint,
        )
    assert_target_checkpoint(
        path,
        expected_checkpoint,
        repo_root=repo_root,
    )
    parent_fd, leaf = open_parent_from_repo_nofollow(repo_root, path)
    try:
        if expected_parent_checkpoint:
            parent_identity = os.fstat(parent_fd)
            expected_parent = expected_parent_checkpoint[-1]
            if (
                parent_identity.st_dev != expected_parent["dev"]
                or parent_identity.st_ino != expected_parent["ino"]
            ):
                raise RuntimeError(
                    f"unlink target parent inode changed: {path}"
                )
        if _capture_target_at(parent_fd, leaf, path) != expected_checkpoint:
            raise RuntimeError(f"unlink target changed before removal: {path}")
        os.unlink(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _assert_ingest_inputs_unchanged(plans: list[IngestPlan]) -> None:
    for plan in plans:
        if plan.input_fingerprint is None or plan.repo_root is None:
            continue
        mapping_path = plan.mapping_path or (
            plan.repo_root
            / "docs"
            / "sources"
            / "ingested-external.skills.json"
        )
        current = capture_ingest_fingerprint(
            skill_dir=plan.skill_md.parent,
            mapping_path=mapping_path,
            repo_root=plan.repo_root,
        )
        if current != plan.input_fingerprint:
            raise RuntimeError(
                f"ingest race detected for {plan.skill_name}; skill sidecars, "
                "mapping, or global claims changed after preflight"
            )


def _materialize_transaction_parents(
    *,
    repo_root: Path,
    targets: dict[Path, list[dict[str, Any]]],
    created: list[Path],
) -> dict[Path, list[dict[str, Any]]]:
    missing: set[str] = {
        str(item["path"])
        for checkpoint in targets.values()
        for item in checkpoint
        if not item["exists"]
    }
    for relative in sorted(
        missing,
        key=lambda value: (len(PurePosixPath(value).parts), value),
    ):
        directory = repo_root.joinpath(*PurePosixPath(relative).parts)
        parent_fd, leaf = open_parent_from_repo_nofollow(
            repo_root,
            directory,
        )
        try:
            try:
                os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(
                    f"transaction parent appeared concurrently: {directory}"
                )
            os.mkdir(leaf, 0o755, dir_fd=parent_fd)
            created.append(directory)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        directory_fd = open_directory_nofollow(directory)
        os.close(directory_fd)

    current: dict[Path, list[dict[str, Any]]] = {}
    for target, expected in targets.items():
        observed = capture_parent_checkpoint(repo_root, target)
        if len(observed) != len(expected):
            raise RuntimeError(
                f"transaction target parent depth changed: {target}"
            )
        for before, after in zip(expected, observed, strict=True):
            if before["exists"] and before != after:
                raise RuntimeError(
                    f"transaction target parent changed: {target}"
                )
            if not before["exists"] and not after["exists"]:
                raise RuntimeError(
                    f"transaction target parent was not created: {target}"
                )
        current[target] = observed
    return current


def _apply_ingest_output_batch(
    *,
    outputs: dict[Path, bytes],
    after_modes: dict[Path, int],
    before: dict[Path, bytes | None],
    checkpoints: dict[Path, dict[str, Any]],
    parent_checkpoints: dict[Path, list[dict[str, Any]]],
    roots: dict[Path, Path | None],
    fault_injector=None,
) -> None:
    """Run the existing hardened writers under an already-durable journal."""
    created_directories: list[Path] = []
    writer_parent_checkpoints = dict(parent_checkpoints)
    try:
        by_root: dict[Path, dict[Path, list[dict[str, Any]]]] = {}
        for path, repo_root in roots.items():
            if repo_root is not None:
                by_root.setdefault(repo_root, {})[path] = parent_checkpoints[path]
        for repo_root, targets in by_root.items():
            writer_parent_checkpoints.update(
                _materialize_transaction_parents(
                    repo_root=repo_root,
                    targets=targets,
                    created=created_directories,
                )
            )
        for path, content in outputs.items():
            _write_atomic_bytes(
                path,
                content,
                repo_root=roots[path],
                expected_checkpoint=checkpoints[path],
                expected_parent_checkpoint=writer_parent_checkpoints.get(path),
                desired_mode=after_modes[path],
            )
            if fault_injector is not None:
                fault_injector("after_replace", path)
    except Exception:
        rollback_errors: list[str] = []
        # A post-rename directory fsync may fail after the destination already
        # changed. Restore every transaction target, not only successful calls.
        for path in reversed(list(outputs)):
            try:
                prior = before[path]
                repo_root = roots[path]
                current_checkpoint = capture_target_checkpoint(
                    path,
                    repo_root=repo_root,
                )
                if prior is None:
                    if current_checkpoint["exists"]:
                        if (
                            current_checkpoint["sha256"]
                            != sha256_bytes(outputs[path])
                            or current_checkpoint["mode"] != after_modes[path]
                        ):
                            raise RuntimeError(
                                f"rollback target changed concurrently: {path}"
                            )
                        _unlink_target_secure(
                            path,
                            repo_root=repo_root,
                            expected_checkpoint=current_checkpoint,
                            expected_parent_checkpoint=(
                                writer_parent_checkpoints.get(path)
                            ),
                        )
                elif (
                    current_checkpoint["exists"]
                    and current_checkpoint["sha256"] == sha256_bytes(prior)
                    and current_checkpoint["mode"] == checkpoints[path]["mode"]
                ):
                    continue
                else:
                    if (
                        not current_checkpoint["exists"]
                        or current_checkpoint["sha256"]
                        != sha256_bytes(outputs[path])
                        or current_checkpoint["mode"] != after_modes[path]
                    ):
                        raise RuntimeError(
                            f"rollback target changed concurrently: {path}"
                        )
                    _write_atomic_bytes(
                        path,
                        prior,
                        repo_root=repo_root,
                        expected_checkpoint=current_checkpoint,
                        expected_parent_checkpoint=(
                            writer_parent_checkpoints.get(path)
                        ),
                        desired_mode=int(checkpoints[path]["mode"]),
                    )
            except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "ingest transaction failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        for directory in sorted(
            created_directories,
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                pass
        raise


def commit_ingest_plans(
    plans: list[IngestPlan],
    *,
    locks_held: bool = False,
    durable_guard: DurableBatchGuard | None = None,
    fault_injector=None,
) -> None:
    """Atomically replace all planned files and restore every prior byte on error."""
    if not plans:
        return
    if not locks_held:
        repo_root = plans[0].repo_root
        if repo_root is not None:
            with acquire_ingest_locks(
                repo_root=repo_root,
                skill_dirs=[plan.skill_md.parent for plan in plans],
                mapping_paths=[
                    plan.mapping_path
                    for plan in plans
                    if plan.mapping_path is not None
                ],
            ) as acquired_guard:
                commit_ingest_plans(
                    plans,
                    locks_held=True,
                    durable_guard=acquired_guard,
                    fault_injector=fault_injector,
                )
                return
    if durable_guard is None:
        repo_roots = {
            Path(plan.repo_root)
            for plan in plans
            if plan.repo_root is not None
        }
        if not repo_roots:
            legacy_targets = [
                target
                for plan in plans
                for target in (plan.skill_md, plan.mapping_path)
                if target is not None
            ]
            if not legacy_targets:
                return
            common = Path(
                os.path.commonpath(
                    [str(Path(target).absolute()) for target in legacy_targets]
                )
            )
            if common in {Path(target).absolute() for target in legacy_targets}:
                common = common.parent
            repo_roots = {common}
            for plan in plans:
                plan.repo_root = common
        if len(repo_roots) != 1:
            raise RuntimeError(
                "durable ingest requires exactly one repository root"
            )
        with durable_batch_lock_and_recover(
            next(iter(repo_roots))
        ) as acquired_guard:
            commit_ingest_plans(
                plans,
                locks_held=True,
                durable_guard=acquired_guard,
                fault_injector=fault_injector,
            )
        return
    _assert_ingest_inputs_unchanged(plans)
    outputs: dict[Path, bytes] = {}
    before: dict[Path, bytes | None] = {}
    checkpoints: dict[Path, dict[str, Any]] = {}
    parent_checkpoints: dict[Path, list[dict[str, Any]]] = {}
    roots: dict[Path, Path | None] = {}
    after_modes: dict[Path, int] = {}
    for plan in plans:
        skill_checkpoint = (
            plan.before_checkpoint
            or capture_target_checkpoint(
                plan.skill_md,
                repo_root=plan.repo_root,
            )
        )
        skill_after_mode = (
            int(plan.after_mode)
            if plan.after_mode is not None
            else int(skill_checkpoint["mode"])
            if skill_checkpoint.get("exists")
            else 0o644
        )
        if (
            plan.before_skill != plan.after_skill
            or not skill_checkpoint.get("exists")
            or skill_checkpoint.get("mode") != skill_after_mode
        ):
            outputs[plan.skill_md] = plan.after_skill
            before.setdefault(plan.skill_md, plan.before_skill)
            roots.setdefault(plan.skill_md, plan.repo_root)
            checkpoints.setdefault(
                plan.skill_md,
                skill_checkpoint,
            )
            after_modes[plan.skill_md] = skill_after_mode
            if plan.repo_root is not None:
                parent_checkpoints.setdefault(
                    plan.skill_md,
                    plan.before_parent_checkpoint
                    or capture_parent_checkpoint(
                        plan.repo_root,
                        plan.skill_md,
                    ),
                )
        if (
            plan.mapping_path is not None
            and plan.after_mapping is not None
            and plan.before_mapping != plan.after_mapping
        ):
            outputs[plan.mapping_path] = plan.after_mapping
            before.setdefault(plan.mapping_path, plan.before_mapping)
            roots.setdefault(plan.mapping_path, plan.repo_root)
            checkpoints.setdefault(
                plan.mapping_path,
                plan.mapping_checkpoint
                or capture_target_checkpoint(
                    plan.mapping_path,
                    repo_root=plan.repo_root,
                ),
            )
            mapping_checkpoint = checkpoints[plan.mapping_path]
            after_modes[plan.mapping_path] = (
                int(mapping_checkpoint["mode"])
                if mapping_checkpoint.get("exists")
                else 0o644
            )
            if plan.repo_root is not None:
                parent_checkpoints.setdefault(
                    plan.mapping_path,
                    plan.mapping_parent_checkpoint
                    or capture_parent_checkpoint(
                        plan.repo_root,
                        plan.mapping_path,
                    ),
                )

    if not outputs:
        return

    # Revalidate the complete batch before the first byte can be replaced.
    for path in outputs:
        repo_root = roots[path]
        if repo_root is not None:
            assert_parent_checkpoint(
                repo_root,
                path,
                parent_checkpoints[path],
            )
        assert_target_checkpoint(
            path,
            checkpoints[path],
            repo_root=roots[path],
        )

    durable_guard.commit_batch(
        outputs,
        lambda: _apply_ingest_output_batch(
            outputs=outputs,
            after_modes=after_modes,
            before=before,
            checkpoints=checkpoints,
            parent_checkpoints=parent_checkpoints,
            roots=roots,
            fault_injector=fault_injector,
        ),
        after_modes=after_modes,
    )


def ingest_one(
    skill_dir: Path,
    source: str,
    source_url: str,
    dry_run: bool,
    *,
    external_mapping: Path | None = None,
    repo_root: Path = REPO_ROOT,
    upstream_path: str | None = None,
    upstream_ref: str = "main",
    resolved_commit: str | None = None,
    path_commit: str | None = None,
    artifact_maps: list[tuple[str, str]] | None = None,
    run_full_validation: bool = True,
) -> bool:
    """Preflight and atomically ingest one skill."""
    lock_mapping = external_mapping or (
        repo_root / "docs" / "sources" / "ingested-external.skills.json"
    )
    try:
        with acquire_ingest_locks(
            repo_root=repo_root,
            skill_dirs=[skill_dir],
            mapping_paths=[lock_mapping],
            durable=not dry_run,
        ) as durable_guard:
            plan = prepare_ingest(
                skill_dir,
                source,
                source_url,
                external_mapping=external_mapping,
                repo_root=repo_root,
                upstream_path=upstream_path,
                upstream_ref=upstream_ref,
                resolved_commit=resolved_commit,
                path_commit=path_commit,
                artifact_maps=artifact_maps,
            )
            action = (
                "external v2 provenance"
                if plan.mapping_path is not None
                else "in-house provenance"
            )
            print(f"  Ingesting: {plan.skill_name} ({skill_dir.parent.name})")
            if run_full_validation:
                if not execute_validated_ingest(
                    [plan],
                    repo_root=repo_root,
                    dry_run=dry_run,
                    locks_held=True,
                    durable_guard=durable_guard,
                ):
                    raise RuntimeError(
                        "isolated full pipeline validation failed"
                    )
                return True
            if dry_run:
                print(
                    f"    [DRY RUN] Preflight passed for {action}; "
                    "zero files written"
                )
                return True
            commit_ingest_plans(
                [plan],
                locks_held=True,
                durable_guard=durable_guard,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"  ERROR: {skill_dir.name} — {exc}", file=sys.stderr)
        return False
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            f"  ERROR: {skill_dir.name} — atomic ingest failed: {exc}",
            file=sys.stderr,
        )
        return False
    if plan.after_skill != plan.before_skill:
        print("    Updated frontmatter")
    if plan.mapping_path is not None:
        print(f"    Registered external v2 provenance: {plan.mapping_path}")
    return True


def pipeline_commands() -> list[list[str]]:
    python = sys.executable or "python"
    return [
        [python, "scripts/enrich_frontmatter.py"],
        [python, "scripts/bootstrap_in_house_sources.py", "--write-json", "docs/sources/in-house.skills.json"],
        [python, "scripts/refresh_repo_views.py"],
        [python, "scripts/generate_tags_index.py"],
        [python, "scripts/build_catalog_json.py"],
        [python, "scripts/check_readme_sync.py"],
        [python, "scripts/lint_skill_quality.py", "--min-lines", "50"],
        [python, "scripts/audit_skill_portfolio.py", "--check-policy"],
        [python, "scripts/audit_licenses.py"],
        [python, "scripts/validate_skill_sources.py"],
        [
            python,
            "scripts/reconcile_artifact_inventory.py",
            "--offline",
            "--check-clean",
            "--quiet",
        ],
        [python, "scripts/check_source_coverage.py", "--min-percent", "100"],
        [python, "-m", "pytest", "-q", "tests"],
    ]


def run_pipeline(
    dry_run: bool,
    *,
    repo_root: Path = REPO_ROOT,
) -> bool:
    """Run the post-ingestion pipeline."""
    if dry_run:
        print(
            "\n[DRY RUN] Pipeline must run in an isolated staging root; "
            "use validate_ingest_plans()."
        )
        return False

    print("\nRunning post-ingestion pipeline...")
    for cmd in pipeline_commands():
        print(f"  Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"    ERROR: command failed to run: {exc}", file=sys.stderr)
            return False
        if result.returncode != 0:
            print(
                f"    ERROR: {' '.join(cmd)} returned {result.returncode}",
                file=sys.stderr,
            )
            if result.stdout:
                print(result.stdout.strip()[-2000:], file=sys.stderr)
            if result.stderr:
                print(result.stderr.strip()[-2000:], file=sys.stderr)
            return False
    return True


IGNORED_STAGE_NAMES = {
    ".git",
    ".hvs-transactions",
    ".pytest_cache",
    "__pycache__",
    ".DS_Store",
}


def _read_regular_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
) -> tuple[bytes, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"non-regular repository entry: {display_path}")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read()
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError(
                f"repository file changed while staging: {display_path}"
            )
        return content, stat.S_IMODE(before.st_mode)
    finally:
        os.close(descriptor)


def _walk_repository_regular_files(
    repo_root: Path,
    *,
    directory_modes: dict[str, int] | None = None,
) -> dict[str, tuple[bytes, int]]:
    """Read a symlink-free repository tree through stable directory fds."""
    results: dict[str, tuple[bytes, int]] = {}
    root_fd = open_directory_nofollow(repo_root)

    def visit(directory_fd: int, relative: Path) -> None:
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            child_relative = relative / name
            metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(
                    f"repository tree contains a symlink: {child_relative}"
                )
            if name in IGNORED_STAGE_NAMES:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if directory_modes is not None:
                    directory_modes[child_relative.as_posix()] = (
                        stat.S_IMODE(metadata.st_mode)
                    )
                child_fd = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (
                        opened.st_dev != metadata.st_dev
                        or opened.st_ino != metadata.st_ino
                    ):
                        raise RuntimeError(
                            "repository directory changed while staging: "
                            f"{child_relative}"
                        )
                    visit(child_fd, child_relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"repository tree contains a non-regular entry: "
                    f"{child_relative}"
                )
            results[child_relative.as_posix()] = _read_regular_at(
                directory_fd,
                name,
                display_path=child_relative,
            )

    try:
        visit(root_fd, Path())
    finally:
        os.close(root_fd)
    return results


def repository_bytes_snapshot(repo_root: Path) -> dict[str, bytes]:
    return {
        relative: content
        for relative, (content, _mode) in _walk_repository_regular_files(
            repo_root
        ).items()
    }


def tracked_report_paths(repo_root: Path) -> set[str]:
    """Return report files explicitly tracked despite the generated-report ignore."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "-z",
                "--",
                "docs/sources/reports",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode != 0:
        return set()
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    }


def transactional_repository_bytes_snapshot(
    repo_root: Path,
    *,
    tracked_reports: set[str],
) -> dict[str, bytes]:
    """Exclude only ignored generated reports from transaction materialization."""
    snapshot = repository_bytes_snapshot(repo_root)
    report_root = PurePosixPath("docs/sources/reports")
    return {
        relative: content
        for relative, content in snapshot.items()
        if not (
            PurePosixPath(relative).parent == report_root
            and PurePosixPath(relative).suffix.lower() in {".json", ".md"}
            and relative not in tracked_reports
        )
    }


def repository_checkpoint_snapshot(
    repo_root: Path,
    relatives: list[str] | set[str],
) -> dict[str, dict[str, Any]]:
    return {
        relative: capture_target_checkpoint(
            repo_root.joinpath(*PurePosixPath(relative).parts),
            repo_root=repo_root,
        )
        for relative in sorted(relatives)
    }


def _copy_stage_repository(repo_root: Path, destination: Path) -> None:
    directory_modes: dict[str, int] = {}
    files = _walk_repository_regular_files(
        repo_root,
        directory_modes=directory_modes,
    )
    destination.mkdir(mode=0o700)
    for relative, mode in sorted(
        directory_modes.items(),
        key=lambda item: (len(PurePosixPath(item[0]).parts), item[0]),
    ):
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.mkdir(mode=mode)
        target.chmod(mode)
    for relative, (content, mode) in files.items():
        target = destination.joinpath(*PurePosixPath(relative).parts)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            target,
            flags,
            mode,
        )
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _translate_plan_to_stage(
    plan: IngestPlan,
    *,
    repo_root: Path,
    stage_root: Path,
) -> IngestPlan:
    skill_relative = plan.skill_md.relative_to(repo_root)
    mapping_relative = (
        plan.mapping_path.relative_to(repo_root)
        if plan.mapping_path is not None
        else None
    )
    return IngestPlan(
        skill_name=plan.skill_name,
        skill_md=stage_root / skill_relative,
        before_skill=(stage_root / skill_relative).read_bytes(),
        after_skill=plan.after_skill,
        after_mode=plan.after_mode,
        mapping_path=stage_root / mapping_relative if mapping_relative else None,
        before_mapping=(
            (stage_root / mapping_relative).read_bytes()
            if mapping_relative
            and (stage_root / mapping_relative).exists()
            else None
        ),
        after_mapping=plan.after_mapping,
        mapping_payload=plan.mapping_payload,
        repo_root=stage_root,
        before_checkpoint=capture_target_checkpoint(
            stage_root / skill_relative,
            repo_root=stage_root,
        ),
        mapping_checkpoint=(
            capture_target_checkpoint(
                stage_root / mapping_relative,
                repo_root=stage_root,
            )
            if mapping_relative
            else None
        ),
        before_parent_checkpoint=capture_parent_checkpoint(
            stage_root,
            stage_root / skill_relative,
        ),
        mapping_parent_checkpoint=(
            capture_parent_checkpoint(
                stage_root,
                stage_root / mapping_relative,
            )
            if mapping_relative
            else None
        ),
    )


def validate_ingest_plans(
    plans: list[IngestPlan],
    *,
    repo_root: Path,
) -> tuple[
    list[IngestPlan],
    dict[str, bytes],
    dict[str, dict[str, Any]],
    set[str],
] | None:
    """Run the complete pipeline in a copy and materialize its exact diff."""
    tracked_reports = tracked_report_paths(repo_root)
    baseline = transactional_repository_bytes_snapshot(
        repo_root,
        tracked_reports=tracked_reports,
    )
    baseline_checkpoints = repository_checkpoint_snapshot(
        repo_root,
        set(baseline),
    )
    baseline_modes: dict[str, int] = {}
    for relative, content in baseline.items():
        checkpoint = baseline_checkpoints[relative]
        if (
            not checkpoint.get("exists")
            or checkpoint.get("sha256") != sha256_bytes(content)
            or not isinstance(checkpoint.get("mode"), int)
        ):
            raise RuntimeError(
                "repository changed while capturing ingest baseline: "
                f"{relative}"
            )
        baseline_modes[relative] = int(checkpoint["mode"])
    # macOS exposes /var as a symlink to /private/var. Resolve the OS-provided
    # temporary root first, then validate and use its symlink-free absolute
    # path so the secure dirfd traversal applies inside staging as well.
    temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    temporary_parent_fd = open_directory_nofollow(temporary_parent)
    os.close(temporary_parent_fd)
    with tempfile.TemporaryDirectory(
        prefix="skill-ingest-stage-",
        dir=temporary_parent,
    ) as temporary:
        stage_root = Path(temporary) / "repo"
        _copy_stage_repository(repo_root, stage_root)
        if (
            transactional_repository_bytes_snapshot(
                repo_root,
                tracked_reports=tracked_reports,
            )
            != baseline
            or repository_checkpoint_snapshot(repo_root, set(baseline))
            != baseline_checkpoints
        ):
            raise RuntimeError("repository changed while staging ingest inputs")
        stage_plans = [
            _translate_plan_to_stage(
                plan,
                repo_root=repo_root,
                stage_root=stage_root,
            )
            for plan in plans
        ]
        commit_ingest_plans(stage_plans)
        if not run_pipeline(False, repo_root=stage_root):
            return None
        staged = transactional_repository_bytes_snapshot(
            stage_root,
            tracked_reports=tracked_reports,
        )
        staged_checkpoints = repository_checkpoint_snapshot(
            stage_root,
            set(staged),
        )
        staged_modes: dict[str, int] = {}
        for relative, content in staged.items():
            checkpoint = staged_checkpoints[relative]
            if (
                not checkpoint.get("exists")
                or checkpoint.get("sha256") != sha256_bytes(content)
                or not isinstance(checkpoint.get("mode"), int)
            ):
                raise RuntimeError(
                    "staged repository changed while capturing pipeline output: "
                    f"{relative}"
                )
            staged_modes[relative] = int(checkpoint["mode"])
        if (
            transactional_repository_bytes_snapshot(
                repo_root,
                tracked_reports=tracked_reports,
            )
            != baseline
            or repository_checkpoint_snapshot(repo_root, set(baseline))
            != baseline_checkpoints
        ):
            raise RuntimeError(
                "repository changed while validating the staged pipeline"
            )
        removed = sorted(set(baseline) - set(staged))
        if removed:
            raise RuntimeError(
                "staged pipeline attempted unmanaged deletions: "
                + ", ".join(removed[:10])
            )
        mutations: list[IngestPlan] = []
        for relative in sorted(staged):
            before = baseline.get(relative)
            after = staged[relative]
            before_mode = baseline_modes.get(relative)
            after_mode = staged_modes[relative]
            if before == after and before_mode == after_mode:
                continue
            target = repo_root / relative
            mutations.append(
                IngestPlan(
                    skill_name=f"staged:{relative}",
                    skill_md=target,
                    before_skill=before,
                    after_skill=after,
                    after_mode=after_mode,
                    repo_root=repo_root,
                    before_checkpoint=(
                        baseline_checkpoints[relative]
                        if relative in baseline_checkpoints
                        else capture_target_checkpoint(
                            target,
                            repo_root=repo_root,
                        )
                    ),
                    before_parent_checkpoint=capture_parent_checkpoint(
                        repo_root,
                        target,
                    ),
                )
            )
        return mutations, baseline, baseline_checkpoints, tracked_reports


def execute_validated_ingest(
    plans: list[IngestPlan],
    *,
    repo_root: Path,
    dry_run: bool,
    locks_held: bool = False,
    durable_guard: DurableBatchGuard | None = None,
) -> bool:
    if not locks_held:
        with acquire_ingest_locks(
            repo_root=repo_root,
            skill_dirs=[plan.skill_md.parent for plan in plans],
            mapping_paths=[
                plan.mapping_path
                for plan in plans
                if plan.mapping_path is not None
            ],
            durable=not dry_run,
        ) as acquired_guard:
            return execute_validated_ingest(
                plans,
                repo_root=repo_root,
                dry_run=dry_run,
                locks_held=True,
                durable_guard=acquired_guard,
            )
    validated = validate_ingest_plans(plans, repo_root=repo_root)
    if validated is None:
        return False
    mutations, baseline, baseline_checkpoints, tracked_reports = validated
    if (
        transactional_repository_bytes_snapshot(
            repo_root,
            tracked_reports=tracked_reports,
        )
        != baseline
        or repository_checkpoint_snapshot(repo_root, set(baseline))
        != baseline_checkpoints
    ):
        raise RuntimeError(
            "repository changed after staged validation; refusing stale apply"
        )
    if dry_run:
        print(
            f"  [DRY RUN] isolated full pipeline passed; "
            f"{len(mutations)} staged file changes, zero repository writes"
        )
        return True
    commit_ingest_plans(
        mutations,
        locks_held=True,
        durable_guard=durable_guard,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register new skills into the repository's provenance system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --dir skills/developer-engineering/vue-expert --source "github:vuejs/vue"
  %(prog)s --batch --dry-run
  %(prog)s --batch --source "community"
        """
    )
    parser.add_argument("--dir", type=Path, help="Path to the skill directory to ingest")
    parser.add_argument("--source", default="in-house",
                        help="Source identifier (in-house, skills.sh, clawhub, github:<owner>/<repo>, community)")
    parser.add_argument("--source-url", default="", help="Original source URL")
    parser.add_argument(
        "--external-mapping",
        type=Path,
        default=Path("docs/sources/ingested-external.skills.json"),
        help="v2 mapping used for newly ingested external skills",
    )
    parser.add_argument(
        "--upstream-path",
        help="Exact upstream SKILL.md path (recommended for external sources)",
    )
    parser.add_argument(
        "--upstream-ref",
        default="main",
        help="Upstream ref recorded for external provenance (default: main)",
    )
    parser.add_argument(
        "--resolved-commit",
        help="Pre-resolved immutable upstream repository commit",
    )
    parser.add_argument(
        "--path-commit",
        help="Pre-resolved immutable commit for the upstream artifact path",
    )
    parser.add_argument(
        "--artifact-map",
        action="append",
        default=[],
        metavar="SOURCE=TARGET",
        help=(
            "Explicit upstream source to canonical target mapping; repeat for "
            "cross-directory sidecars"
        ),
    )
    parser.add_argument(
        "--artifact-manifest",
        type=Path,
        help="JSON artifact mapping list or object containing artifacts[]",
    )
    parser.add_argument("--batch", action="store_true",
                        help="Batch ingest all untracked skills")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing")
    parser.add_argument("--skip-pipeline", action="store_true",
                        help="Deprecated; safety gates always run in staging")
    args = parser.parse_args(argv)
    if args.skip_pipeline:
        print(
            "WARNING: --skip-pipeline is deprecated and ignored; all safety "
            "gates run in isolated staging.",
            file=sys.stderr,
        )

    if not args.dir and not args.batch:
        parser.error("Specify --dir <path> for single ingestion or --batch for all untracked skills")

    success_count = 0
    fail_count = 0
    external_mapping = (
        args.external_mapping
        if args.external_mapping.is_absolute()
        else REPO_ROOT / args.external_mapping
    )
    if args.batch:
        try:
            preliminary_tracked = get_tracked_skills(
                REPO_ROOT / "docs" / "sources"
            )
            candidate_skill_dirs = find_untracked_skills(
                SKILLS_DIR, preliminary_tracked
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot inventory v2 claims: {exc}", file=sys.stderr)
            return 1
    else:
        candidate_skill_dirs = [
            REPO_ROOT / args.dir
            if not args.dir.is_absolute()
            else args.dir
        ]
    manifest_path = None
    if args.artifact_manifest:
        manifest_path = (
            args.artifact_manifest
            if args.artifact_manifest.is_absolute()
            else REPO_ROOT / args.artifact_manifest
        )
    try:
        with acquire_ingest_locks(
            repo_root=REPO_ROOT,
            skill_dirs=candidate_skill_dirs,
            mapping_paths=[external_mapping],
            durable=not args.dry_run,
        ) as durable_guard:
            artifact_maps = [
                parse_artifact_map(item) for item in args.artifact_map
            ]
            manifest_before = (
                manifest_path.read_bytes() if manifest_path is not None else None
            )
            if manifest_path is not None:
                artifact_maps.extend(load_artifact_manifest(manifest_path))
            targets = [target for _, target in artifact_maps]
            if len(targets) != len(set(targets)):
                raise ValueError("artifact mappings contain duplicate targets")

            if args.batch:
                tracked = get_tracked_skills(REPO_ROOT / "docs" / "sources")
                skill_dirs = find_untracked_skills(SKILLS_DIR, tracked)
                if {
                    path.resolve() for path in skill_dirs
                } != {
                    path.resolve() for path in candidate_skill_dirs
                }:
                    raise RuntimeError(
                        "global claims changed before batch lock acquisition; "
                        "retry"
                    )
                print(
                    f"Found {len(skill_dirs)} untracked skills "
                    f"(out of {len(tracked)} tracked)"
                )
            else:
                skill_dirs = candidate_skill_dirs

            plans: list[IngestPlan] = []
            virtual_mapping: dict[str, Any] | None = None
            for skill_dir in skill_dirs:
                try:
                    plan = prepare_ingest(
                        skill_dir,
                        args.source,
                        args.source_url,
                        external_mapping=external_mapping,
                        repo_root=REPO_ROOT,
                        upstream_path=args.upstream_path,
                        upstream_ref=args.upstream_ref,
                        resolved_commit=args.resolved_commit,
                        path_commit=args.path_commit,
                        artifact_maps=artifact_maps,
                        existing_mapping_payload=virtual_mapping,
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(
                        f"  ERROR: {skill_dir.name} — {exc}",
                        file=sys.stderr,
                    )
                    fail_count += 1
                    continue
                plans.append(plan)
                if plan.mapping_payload is not None:
                    virtual_mapping = plan.mapping_payload
                success_count += 1

            if fail_count:
                raise ValueError(
                    "preflight failed; no skill or mapping files were written"
                )
            if virtual_mapping is not None:
                final_mapping = _json_bytes(virtual_mapping)
                for plan in plans:
                    if plan.mapping_path == external_mapping:
                        plan.after_mapping = final_mapping
            if (
                manifest_path is not None
                and manifest_path.read_bytes() != manifest_before
            ):
                raise RuntimeError("artifact manifest changed during preflight")
            _assert_ingest_inputs_unchanged(plans)
            if not execute_validated_ingest(
                plans,
                repo_root=REPO_ROOT,
                dry_run=args.dry_run,
                locks_held=True,
                durable_guard=durable_guard,
            ):
                raise RuntimeError("isolated full pipeline validation failed")
    except (
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: ingest transaction aborted: {exc}", file=sys.stderr)
        return 1

    print(f"\nIngestion complete: {success_count} succeeded, {fail_count} failed")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

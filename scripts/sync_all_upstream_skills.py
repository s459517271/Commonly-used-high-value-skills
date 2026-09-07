#!/usr/bin/env python3
"""Synchronize every auto-upgradeable upstream skill source.

This is the one-command entrypoint for routine repository maintenance:

1. check or apply every provenance-v2 artifact set through one governed engine
2. optionally run the repository generation, lint, and test pipeline

Installation is intentionally outside this repository-maintenance command.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable or "python"
POST_SYNC_PIPELINE = (
    (PYTHON, "scripts/enrich_frontmatter.py"),
    (PYTHON, "scripts/bootstrap_in_house_sources.py", "--write-json", "docs/sources/in-house.skills.json"),
    (PYTHON, "scripts/refresh_repo_views.py"),
    (PYTHON, "scripts/generate_tags_index.py"),
    (PYTHON, "scripts/build_catalog_json.py"),
    (PYTHON, "scripts/check_readme_sync.py"),
    (PYTHON, "scripts/generate_sources_index.py"),
    (PYTHON, "scripts/lint_skill_quality.py", "--min-lines", "50"),
    (PYTHON, "scripts/validate_skill_sources.py"),
    (
        PYTHON,
        "scripts/reconcile_artifact_inventory.py",
        "--offline",
        "--check-clean",
        "--quiet",
    ),
    (PYTHON, "scripts/check_source_coverage.py", "--min-percent", "100"),
    (PYTHON, "scripts/audit_skill_portfolio.py", "--check-policy"),
    (PYTHON, "scripts/audit_licenses.py"),
    (PYTHON, "-m", "pytest", "-q", "tests"),
)


def run(command: tuple[str, ...] | list[str], *, repo_root: Path) -> int:
    print("Running:", " ".join(command))
    result = subprocess.run(command, cwd=repo_root, check=False)
    return result.returncode


def repository_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(repo_root.rglob("*")):
        relative = path.relative_to(repo_root)
        if any(
            part in {".git", ".pytest_cache", "__pycache__"}
            for part in relative.parts
        ):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync all externally tracked upstream skills.")
    parser.add_argument("--apply", action="store_true", help="Write updates. Without this, performs check/dry-run style commands.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="Repository root.")
    parser.add_argument("--run-pipeline", action="store_true", help="Run generation, lint, and tests after syncing.")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    before = repository_digest(repo_root) if args.apply else None

    if args.apply:
        sync_state = run(
            (
                PYTHON,
                "scripts/sync_upstream.py",
                "--apply",
            ),
            repo_root=repo_root,
        )
    else:
        sync_state = run(
            (
                PYTHON,
                "scripts/sync_upstream.py",
                "--check-only",
            ),
            repo_root=repo_root,
        )

    if sync_state not in {0, 1, 2}:
        sync_state = 1
    wrote_files = (
        args.apply
        and before is not None
        and repository_digest(repo_root) != before
    )
    if args.apply and args.run_pipeline and (sync_state == 0 or wrote_files):
        for command in POST_SYNC_PIPELINE:
            if run(command, repo_root=repo_root) != 0:
                return 1

    return sync_state


if __name__ == "__main__":
    raise SystemExit(main())

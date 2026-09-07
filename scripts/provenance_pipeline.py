#!/usr/bin/env python3
"""Unified provenance pipeline runner.

Goal: replace patchwork command chains with one stable entrypoint.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def resolve_python_cmd() -> list[str]:
    """Use the current interpreter so nested subprocesses work cross-platform."""
    return [sys.executable] if sys.executable else ["python"]


def run(
    cmd: list[str],
    root: Path,
    *,
    accepted_codes: frozenset[int] = frozenset({0}),
) -> None:
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, cwd=root, check=False)
    if result.returncode not in accepted_codes:
        raise subprocess.CalledProcessError(result.returncode, cmd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["all", "quick"], default="all")
    parser.add_argument("--config", default="docs/sources/provenance.config.json")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cfg = json.loads((root / args.config).read_text(encoding="utf-8"))
    p = cfg["paths"]
    stale_days = str(cfg.get("stale_days", 30))
    coverage = str(cfg.get("coverage_min_percent", 100))
    python_cmd = resolve_python_cmd()

    run(python_cmd + ["scripts/bootstrap_in_house_sources.py", "--write-json", p["in_house_mapping"]], root)
    run(python_cmd + ["scripts/validate_skill_sources.py"], root)
    run(
        python_cmd
        + [
            "scripts/reconcile_artifact_inventory.py",
            "--offline",
            "--check-clean",
            "--quiet",
        ],
        root,
    )
    run(python_cmd + ["scripts/audit_licenses.py"], root)
    run(python_cmd + ["scripts/check_source_coverage.py", "--min-percent", coverage], root)
    run(
        python_cmd
        + ["scripts/skills_refresh_planner.py", "--stale-days", stale_days, "--write-json", p["refresh_queue"]],
        root,
    )
    run(python_cmd + ["scripts/build_skills_catalog.py", "--write-json", p["catalog"]], root)
    run(python_cmd + ["scripts/generate_sources_index.py", "--write-json", p["sources_index"]], root)

    if args.mode == "all":
        run(
            python_cmd
            + ["scripts/skills_bulk_update_stub.py", "--queue", p["refresh_queue"], "--write-plan", p["bulk_plan"]],
            root,
        )
        # The deterministic offline inventory is deliberately degraded rather
        # than pretending to be a fresh upstream check. Code 2 is therefore an
        # expected report state here, while every actual gate remains fail-fast.
        run(
            python_cmd
            + [
                "scripts/check_upstream_github_updates.py",
                "--write-json",
                p["upstream_check"],
            ],
            root,
            accepted_codes=frozenset({0, 2}),
        )

    print("Provenance pipeline completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

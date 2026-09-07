from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_intentionally_vulnerable_samples_are_not_live_dependency_manifests() -> None:
    canonical = ROOT / "skills" / "developer-engineering" / "dependency-auditor"
    exported = ROOT / "openclaw-skills" / "dependency-auditor"

    for root in (canonical, exported):
        assert not (root / "assets" / "sample_requirements.txt").exists()
        assert not (root / "test-project" / "package.json").exists()
        requirements_fixture = root / "assets" / "sample_requirements.fixture"
        package_fixture = root / "test-project" / "package.vulnerable.fixture.json"
        assert requirements_fixture.is_file()
        assert package_fixture.is_file()
        assert requirements_fixture.read_text(encoding="utf-8").strip()
        assert isinstance(json.loads(package_fixture.read_text(encoding="utf-8")), dict)

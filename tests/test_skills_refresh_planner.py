import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "skills_refresh_planner.py"


def load_module():
    spec = importlib.util.spec_from_file_location("skills_refresh_planner", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SkillsRefreshPlannerTests(unittest.TestCase):
    def test_archived_and_local_only_sources_do_not_enter_refresh_queue(self):
        module = load_module()
        today = date(2026, 7, 27)
        base = {
            "video_name": "local helper",
            "normalized_slug": "local-helper",
            "status": "verified_in_repo",
            "repo_skill": "skills/example/local-helper/SKILL.md",
            "source": "https://example.com",
        }

        for upstream in [
            {
                "repo": "owner/repo",
                "sync_mode": "archived",
                "last_checked_at": "2025-01-01",
                "last_synced_at": "2025-01-01",
            },
            {
                "repo": "owner/repo",
                "sync_mode": "local-only",
                "last_checked_at": "2025-01-01",
                "last_synced_at": "2025-01-01",
            },
            {
                "repo": "local-repo/in-house",
                "last_checked_at": "2025-01-01",
                "last_synced_at": "2025-01-01",
            },
        ]:
            skill = dict(base)
            skill["upstream"] = upstream
            self.assertIsNone(
                module.evaluate_item(
                    "source.skills.json",
                    None,
                    skill,
                    today,
                    stale_days=30,
                )
            )

    def test_stale_active_external_source_enters_queue(self):
        module = load_module()
        item = module.evaluate_item(
            "source.skills.json",
            None,
            {
                "video_name": "external helper",
                "normalized_slug": "external-helper",
                "status": "verified_in_repo",
                "repo_skill": "skills/example/external-helper/SKILL.md",
                "source": "https://example.com",
                "upstream": {
                    "repo": "owner/repo",
                    "last_checked_at": "2026-01-01",
                    "last_synced_at": "2026-01-01",
                },
            },
            date(2026, 7, 27),
            stale_days=30,
        )

        self.assertIsNotNone(item)
        self.assertIn("stale_last_checked>30d", item.reasons)


if __name__ == "__main__":
    unittest.main()

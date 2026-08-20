import hashlib
import json
import stat
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = REPO_ROOT / "docs/sources/larksuite-cli-2026-05.skills.json"
REVIEWED_COMMIT = "ca35f6061616d4f47681368bbbef03be28193dc9"
PREVIOUS_REVIEWED_COMMIT = "755daa4de3ea12785c43a15244ffb8f012122c13"
EXPECTED_COUNTS = {
    "lark-approval": 17,
    "lark-attendance": 1,
    "lark-base": 30,
    "lark-calendar": 11,
    "lark-contact": 4,
    "lark-doc": 44,
    "lark-drive": 61,
    "lark-event": 8,
    "lark-im": 59,
    "lark-mail": 34,
    "lark-markdown": 6,
    "lark-minutes": 10,
    "lark-okr": 19,
    "lark-openapi-explorer": 1,
    "lark-shared": 7,
    "lark-sheets": 27,
    "lark-skill-maker": 1,
    "lark-slides": 50,
    "lark-task": 18,
    "lark-vc": 8,
    "lark-vc-agent": 3,
    "lark-whiteboard": 31,
    "lark-wiki": 14,
    "lark-workflow-meeting-summary": 1,
    "lark-workflow-standup-report": 1,
}
EXPECTED_PATH_COMMITS = {
    "lark-approval": "1efe2dfb3304a7cac2a70e4adecfb1dc888ebe06",
    "lark-attendance": "69ae326d01a9163ca22408c746e052003cf0af2c",
    "lark-base": "9b231d98253710219e26b902f5683d3e7f32bc92",
    "lark-calendar": "b75632e78a78e0b32b762bba659efaf72c797d4e",
    "lark-contact": "87be09ef5f227c7b63d5eba40649544b5bec0133",
    "lark-doc": "525a98270f80693bdaf3c0a6006e9f3f94820851",
    "lark-drive": "5028c2e6ffb9e2ed8e56fed688f1a1dd74e6b11a",
    "lark-event": "fcdef499bb23739b720c665d49875a9957c97d48",
    "lark-im": "f98db395d8520e4a431ed714261ab899e562aa45",
    "lark-mail": "2a1613484ad6cbf4057b044f5ee2138f34314bbb",
    "lark-markdown": "f98dbfe247c6ec172715ce578b67ac6fa22655db",
    "lark-minutes": "327874c8f4af85586af97ddda6f1ac4bd168e79a",
    "lark-okr": "409a3172da5d43c98bbb637557f3d10403febaf0",
    "lark-openapi-explorer": "83dfb068ad8bb4052787d80ca415118a20849b85",
    "lark-shared": "327874c8f4af85586af97ddda6f1ac4bd168e79a",
    "lark-sheets": "be2a96f490b5356a004bafffcaa39daab6179d76",
    "lark-skill-maker": "83dfb068ad8bb4052787d80ca415118a20849b85",
    "lark-slides": "0c5530dc63b65b3fda86f667f5725b1a08f0c4dc",
    "lark-task": "b9cced677a89fd42317b386daff41a9f62b473c3",
    "lark-vc": "327874c8f4af85586af97ddda6f1ac4bd168e79a",
    "lark-vc-agent": "841953496b41a06bb670396f3d9f8fba943766ed",
    "lark-whiteboard": "27ab8fbea3e6f2b07e93a26bc635e0e52023d7a0",
    "lark-wiki": "6e2cad7221755d3668b350f186b862d64e0cba97",
    "lark-workflow-meeting-summary": PREVIOUS_REVIEWED_COMMIT,
    "lark-workflow-standup-report": "049ddf771b435e86a4f5a71e616336ec44341160",
}
LOCAL_OVERLAYS = {
    "lark-approval": {
        "skills/knowledge-and-pm-integrations/lark-approval/"
        "references/lark-approval-instance-form-control-parameters.md",
    },
    "lark-mail": {
        "skills/knowledge-and-pm-integrations/lark-mail/"
        "references/lark-mail-html.md",
    },
    "lark-minutes": {
        "skills/knowledge-and-pm-integrations/lark-minutes/"
        "references/lark-minutes-todo.md",
    },
    "lark-okr": {
        "skills/knowledge-and-pm-integrations/lark-okr/"
        "references/lark-okr-indicator-update.md",
        "skills/knowledge-and-pm-integrations/lark-okr/"
        "references/lark-okr-indicators.md",
        "skills/knowledge-and-pm-integrations/lark-okr/"
        "references/lark-okr-progress-list.md",
    },
    "lark-slides": {
        "skills/knowledge-and-pm-integrations/lark-slides/"
        "references/visual-planning.md",
    },
    "lark-vc": {
        "skills/knowledge-and-pm-integrations/lark-vc/"
        "references/lark-vc-meeting-events.md",
        "skills/knowledge-and-pm-integrations/lark-vc/"
        "references/lark-vc-search.md",
    },
}


def _mode(path: Path) -> str:
    return "100755" if stat.S_IMODE(path.stat().st_mode) & 0o111 else "100644"


def test_lark_complete_directory_mirrors_are_exact_and_owned() -> None:
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    entries = {entry["normalized_slug"]: entry for entry in mapping["skills"]}
    assert set(entries) == set(EXPECTED_COUNTS)

    for slug, expected_count in EXPECTED_COUNTS.items():
        entry = entries[slug]
        origin = entry["origins"][0]
        source_root = f"skills/{slug}"
        target_root = f"skills/knowledge-and-pm-integrations/{slug}"
        canonical_root = REPO_ROOT / target_root

        assert entry["kind"] == (
            "overlay" if slug in LOCAL_OVERLAYS else "mirror"
        )
        assert entry["sync_mode"] == "monitor"
        assert len(entry["origins"]) == (
            2 if slug in LOCAL_OVERLAYS else 1
        )
        assert origin["path"] == source_root
        assert origin["sync_mode"] == "monitor"
        if slug in LOCAL_OVERLAYS:
            local_targets = LOCAL_OVERLAYS[slug]
            claimed_targets = {
                artifact["target"] for artifact in origin["artifacts"]
            }
            assert local_targets.isdisjoint(claimed_targets)
            assert len(claimed_targets) == expected_count - len(local_targets)
            local_origin = entry["origins"][1]
            assert local_origin == {
                "repo": "local-repo/curation",
                "path": target_root,
                "license": None,
                "sync_mode": "local-only",
                "artifacts": [
                    {
                        "source": local_target,
                        "target": local_target,
                        "type": "file",
                    }
                    for local_target in sorted(local_targets)
                ],
                "tracking": {
                    "channel": "local",
                    "ref": "local",
                    "resolved_commit": None,
                    "path_commit": None,
                    "content_sha256": None,
                    "last_checked_at": "2026-08-20",
                    "last_synced_at": "2026-08-20",
                },
            }
        else:
            assert origin["artifacts"] == [
                {
                    "source": source_root,
                    "target": target_root,
                    "type": "directory",
                }
            ]
        assert origin["tracking"]["channel"] == "default_branch"
        assert origin["tracking"]["ref"] == "main"
        assert entry["upstream"]["last_synced_commit"] == REVIEWED_COMMIT
        assert entry["upstream"]["path_commit"] == EXPECTED_PATH_COMMITS[slug]
        assert origin["tracking"]["resolved_commit"] == REVIEWED_COMMIT
        assert origin["tracking"]["path_commit"] == EXPECTED_PATH_COMMITS[slug]

        actual = {
            path.relative_to(REPO_ROOT).as_posix(): path
            for path in canonical_root.rglob("*")
            if path.is_file()
        }
        managed = {item["path"]: item for item in entry["managed_files"]}
        assert len(actual) == expected_count
        assert set(actual) == set(managed)
        for repo_path, path in actual.items():
            record = managed[repo_path]
            assert record["owner"] == slug
            assert record["mode"] == _mode(path)
            assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_lark_license_lineage_is_locked_to_reviewed_commit() -> None:
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    expected = {
        "path": "LICENSE",
        "blob_sha": "2b8bbb5e9c46b1abc3a5ac68ee190baf046ba968",
        "content_sha256": (
            "c969fc7e3af68e6bf40b0d8dd9c3dcc377eb685a2139535b203b39fdcad739ee"
        ),
        "spdx": "MIT",
        "resolved_commit": REVIEWED_COMMIT,
        "api_spdx": "MIT",
    }
    for entry in mapping["skills"]:
        assert entry["origins"][0]["tracking"]["license_checkpoint"] == expected


def test_lark_whitespace_adaptations_are_scoped_and_clean() -> None:
    adapted = {
        "skills/knowledge-and-pm-integrations/lark-calendar/SKILL.md",
        *{
            path
            for paths in LOCAL_OVERLAYS.values()
            for path in paths
        },
    }
    for repo_path in adapted:
        lines = (REPO_ROOT / repo_path).read_text(encoding="utf-8").splitlines()
        assert all(line == line.rstrip() for line in lines)

    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    attempts = mapping["verification_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["target"] == (
        f"larksuite/cli@{PREVIOUS_REVIEWED_COMMIT}"
    )
    assert "whitespace-only" in attempts[0]["evidence"]
    assert attempts[1]["target"] == (
        "larksuite/cli@"
        f"{PREVIOUS_REVIEWED_COMMIT}..{REVIEWED_COMMIT}"
    )
    assert "outside the 25 declared artifact sets" in attempts[1]["evidence"]

# Upstream Sync Runbook

This repository supports two levels of automated upstream refresh.

For the full weekly curation workflow, including portfolio audit, external
blocker triage, replacement decisions, and automation model settings, see
[Skill Curation and Automation Runbook](./skill-curation-and-automation-runbook.md).

## Sync Everything

Use this for routine maintenance when upstream repositories may have changed:

```bash
python scripts/sync_all_upstream_skills.py --apply --run-pipeline
```

This command checks every active external canonical skill artifact set and
refreshes only the sources whose provenance policy permits automatic
replacement:

- every active external canonical skill in `*.skills.json` is checked
- only stable-release or immutable-commit sources approved for `replace`
- generated repository views, OpenClaw export, tag index, catalog, quality lint, and tests

Managed bundles in `*.bundle.json` are intentionally outside this command.
They must be checked with their bundle-specific release, package-integrity,
inventory, and installer validation.

Repository refresh and client installation are separate transactions. After
the sync and full pipeline pass, use the governed installer explicitly:

```bash
node bin/install-skills.js install --target codex
```

## Check Everything Without Writing

```bash
python scripts/sync_upstream.py --check-only \
  --report-json /tmp/upstream-report.json
```

Exit status `0` means `complete`, `2` means `degraded` (manual monitor review
or rollback review is required), and `1` means `failed`. A failed or incomplete
network scan must never be reported as “all up to date”.

## Sync One Source

For any provenance-backed canonical source, use exact source filtering:

```bash
python scripts/sync_upstream.py --check-only --source github:obra/superpowers
python scripts/sync_upstream.py --apply --source github:obra/superpowers
```

Default-branch sources such as `addyosmani/agent-skills` and
`simota/agent-skills` remain monitor-only. Review their commit range and
artifact inventory explicitly; the all-source command does not bypass that
policy through a legacy importer.

## What Makes A Skill Auto-Upgradeable

Provenance v2 is authoritative. An external artifact set is auto-upgradeable
only when it has an approved stable channel and complete immutable checkpoints:

```json
{
  "repo_skill": "skills/<category>/<skill>/SKILL.md",
  "kind": "mirror",
  "sync_mode": "replace",
  "origins": [{
    "repo": "owner/repo",
    "sync_mode": "replace",
    "artifacts": [{
      "source": "path/in/upstream",
      "target": "skills/<category>/<skill>",
      "type": "directory"
    }],
    "tracking": {
      "channel": "latest_release",
      "ref": "v1.2.3",
      "resolved_commit": "<full commit>",
      "path_commit": "<full commit>",
      "content_sha256": "<sha256>"
    }
  }]
}
```

`default_branch`, canary, movable aliases, and composite dependencies are
monitor-only. A fixed ref is auto-replaceable only when the ref itself is the
full immutable commit.

The generic sync materializes every declared file or directory artifact as
binary bytes, preserves repository frontmatter on the canonical `SKILL.md`,
and applies the complete set through a staged transaction. It may replace or
prune only files owned by the selected origin whose current digest still
matches `managed_files`. Files owned by local curation or another origin are
protected.

Before adopting historical sidecars, classify their ownership:

```bash
python scripts/reconcile_artifact_inventory.py \
  --output /tmp/artifact-inventory.json
python scripts/reconcile_artifact_inventory.py \
  --write --output /tmp/artifact-inventory-write.json
```

Only an exact blob match at the locked commit becomes an external artifact.
Everything else must be an explicit local overlay or remain blocked for review.

The machine report always satisfies:

```text
total = equal + changed + monitor_review + unavailable + rollback + expected_skipped
```

## Handling Noisy Or Retired Upstreams

Do not treat every upstream fetch failure as a repository regression.

- ClawHub SSL EOF, TLS handshake timeouts, and transient `IncompleteRead` errors are external noise. Retry briefly, record source health, and avoid marking the run as fully fresh if discovery was partial.
- Old GitHub path `404` responses usually mean the provenance path moved or was archived. Exact blob/tree move candidates may be reported, but the synchronizer never guesses or applies a rename automatically.
- If the upstream skill has genuinely disappeared but the local skill remains valuable, keep the local version and mark the mapping as archived or local-only instead of repeatedly failing future sync runs.
- License gaps are not noise. Missing or unknown license metadata must be fixed, or the skill must become an original in-house rewrite before copying any upstream text.

Example archived v2 origin:

```json
{
  "kind": "snapshot",
  "sync_mode": "archived",
  "upstream": {
    "sync_mode": "archived",
    "archived_at": "2026-06-29"
  },
  "origins": [{
    "repo": "owner/repo",
    "sync_mode": "archived",
    "tracking": {
      "channel": "fixed_ref",
      "ref": "<immutable-commit>",
      "resolved_commit": "<immutable-commit>"
    }
  }]
}
```

Example local-only v2 snapshot:

```json
{
  "kind": "snapshot",
  "sync_mode": "local-only",
  "upstream": {
    "sync_mode": "local-only"
  },
  "origins": [{
    "repo": "owner/repo",
    "sync_mode": "local-only",
    "tracking": {
      "channel": "fixed_ref",
      "ref": "<immutable-commit>",
      "resolved_commit": "<immutable-commit>"
    }
  }]
}
```

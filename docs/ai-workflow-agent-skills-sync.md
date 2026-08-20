# AI Workflow Agent Skills Sync

`skills/ai-workflow/` is the local AI workflow category for engineering-agent skills. It combines existing local workflow skills with a full import of [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills).

## Refresh From Upstream

```bash
python scripts/sync_upstream.py --check-only \
  --source github:addyosmani/agent-skills \
  --report-json /tmp/addyosmani-upstream.json
```

This source follows a default branch, so it is deliberately monitor-only. The
check inspects every declared artifact at one immutable commit and reports a
commit-range review; it does not overwrite the curated collection.

After reviewing the upstream commit range, update exact external artifacts and
local curation overlays in a focused PR, then verify ownership:

```bash
python scripts/reconcile_artifact_inventory.py \
  --mapping docs/sources/addyosmani-agent-skills-2026-04.skills.json \
  --output /tmp/addyosmani-inventory.json
```

Do not use the historical bulk importer to bypass the monitor policy.

For all upstream-backed canonical skills across the whole repository, use:

```bash
python scripts/sync_all_upstream_skills.py --apply --run-pipeline
```

## Install Locally For Codex

After the repository pipeline has run, sync the curated skills into the local Codex skill root:

```bash
node bin/install-skills.js install --target codex
```

OpenClaw should continue to use the generated flat export:

```text
openclaw-skills/
```

## Upstream Bundle

The upstream project includes Claude Code slash commands, plugin metadata,
specialist personas, hooks, and setup docs. The retained historical bundle and
repository-specific supplements live under:

```text
skills/ai-workflow/using-agent-skills/upstream-bundle/
```

Its files are explicitly owned as external exact artifacts or
`local-repo/curation` overlays. Do not change ownership by refreshing hashes;
review the source range and update the provenance artifact mapping instead.

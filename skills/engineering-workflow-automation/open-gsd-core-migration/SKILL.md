---
name: open-gsd-core-migration
description: "Audit, back up, migrate, verify, and safely retire legacy get-shit-done or @gsd-build installations when moving clients to the repository-pinned Open GSD Core managed bundle."
zh_description: "迁移旧 get-shit-done 或 @gsd-build 安装：先审计和哈希备份，再安装仓库锁定的 Open GSD Core，验证后按所有权安全清理并支持回滚。"
version: "1.0.0"
author: seaworld008
source: in-house
source_url: ""
tags: [open-gsd, migration, backup, rollback, cleanup]
created_at: "2026-08-20"
updated_at: "2026-08-20"
quality: 4
complexity: advanced
---

# Open GSD Core Migration

## Purpose

Use this skill to replace legacy GSD installations without losing user changes
or mixing files owned by different installers.

The workflow is evidence-driven:

1. inventory;
2. backup and hash;
3. install from the repository-pinned managed bundle;
4. verify one client at a time;
5. prune only proven legacy ownership;
6. retain a deterministic rollback record.

## When to Use

Trigger this workflow when a client contains one or more of:

- `get-shit-done/` directories;
- legacy GSD slash commands, hooks, agents, or shared runtime files;
- global `@gsd-build/sdk`;
- unmanaged or conflicting `gsd-*` skill copies;
- an old installer manifest;
- retired Hermes + Graphify + GSD wrapper skills;
- different same-name GSD skill bytes in overlapping discovery roots.

Do not use it for a clean first-time Open GSD Core installation.

## Non-Negotiable Safety Rules

- Never remove a path solely because its name begins with `gsd-`.
- Never trust modification time as ownership proof.
- Never overwrite a user-modified file without archiving its bytes first.
- Never mix Core `.planning/` state with Pi `.gsd/` state.
- Never touch a backup-only Antigravity directory.
- Never run an optional Pi or Hermes runtime installer as a side effect.
- Never upgrade a system Graphify executable as part of this migration.

## Phase 1: Inventory

Record each active client root and discovery path before mutation.

The inventory should include:

- canonical absolute path;
- file type and mode;
- SHA-256 for each regular file;
- symlink target without following it;
- owning legacy manifest when available;
- current official manifest when available;
- whether another active client discovers the same path;
- whether Git or another package manager owns the file.

Also record:

- relevant hooks and configuration fragments;
- global npm packages matching the legacy GSD namespace;
- `.planning/` and `.gsd/` project-state roots;
- every same-name `gsd-*` skill across client roots.

## Phase 2: Backup

Create a timestamped, user-private backup before installing or pruning.

The backup manifest must distinguish:

```json
{
  "path": "/absolute/path/to/file",
  "sha256": "hex digest",
  "classification": "managed-unmodified | managed-modified | unowned",
  "consumer": "codex | claude | cursor | antigravity",
  "action": "replace | archive | preserve"
}
```

Back up configuration and hooks separately from generated skill payloads so a
rollback can restore user configuration without reviving retired skill copies.

If a pre-migration snapshot is unavailable, state that limitation explicitly.
Do not invent old hashes from current files.

## Phase 3: Build the Install Plan

Read the active Open GSD Core bundle metadata from repository provenance.
The bundle metadata, not this skill, owns:

- the stable package version;
- tarball integrity;
- release and attestation commits;
- supported runtimes;
- expected inventory counts;
- official installer entrypoint.

For each client, prepare a plan containing:

- selected runtime and scope;
- resolved configuration root;
- expected official manifest location;
- files the installer may replace;
- legacy candidates to evaluate only after verification;
- configuration fragments that must be merged rather than replaced.

## Phase 4: Install Sequentially

Install one client at a time. Keep later clients untouched until the current
client passes its verification gate.

Recommended order:

1. Claude;
2. Cursor;
3. each active Antigravity root independently;
4. Codex last.

This order reduces shared-skill interference and leaves the primary Codex
environment available for diagnosis until the end.

## Phase 5: Verify

For every client, verify:

- official installer version and runtime;
- official managed manifest schema;
- every owned file hash and mode;
- expected skill and agent counts;
- runtime health command registration;
- onboarding command registration;
- tool-discovery command registration;
- preservation of non-GSD configuration;
- absence of content-different same-name GSD skills in that client's discovery
  scope.

Re-run the same install plan and compare the owned-file set. Ignore only
documented timestamps; any content, ownership, or mode change is not idempotent.

## Phase 6: Classify Legacy Files

Classify every cleanup candidate:

### Managed and unmodified

The legacy manifest identifies the file and its recorded digest matches the
current bytes. It may be removed after the replacement client passes.

### Managed but modified

The legacy manifest identifies the file, but the digest differs. Archive it
with metadata, then remove it from active discovery only after confirming the
new runtime covers the intent.

### Unowned or ambiguous

No trustworthy manifest proves ownership. Preserve the original bytes in the
timestamped backup and move them out of active discovery; do not delete them.

## Phase 7: Prune

Eligible cleanup includes:

- legacy `get-shit-done/` payloads;
- legacy GSD commands and hooks;
- duplicated shared `gsd-*` skills superseded by the official manifest;
- the legacy global npm package after client verification;
- retired composite skill copies.

Pruning must use the recorded classification. A broad glob is not a cleanup
plan.

## Rollback

Rollback is client-scoped:

1. stop further migrations;
2. preserve the failed client's new manifest and logs;
3. restore archived configuration and modified/unowned files;
4. restore legacy managed files from the backup only when their recorded
   digests match the backup manifest;
5. remove new owned files using the new official manifest;
6. verify the restored discovery graph and commands.

Do not roll back clients that already passed unless shared-root evidence shows
they were affected.

## Completion Gate

Migration is complete only when:

- each active client has a verified official manifest;
- all non-GSD configuration is preserved;
- every legacy candidate has a recorded action and evidence;
- retired composite skills are absent from active discovery;
- no client sees two different contents for the same skill name;
- a second install plan produces zero owned-file changes;
- backup and rollback manifests remain readable.

## Boundaries

This skill defines migration decisions. The repository installer implements
the managed bundle selection, inventory, and prune mechanics. If the installer
cannot prove ownership, this workflow requires archive or preservation rather
than deletion.

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATE_NOTION = (
    ROOT
    / "skills"
    / "growth-operations-xiaohongshu"
    / "linkedin-growth"
    / "scripts"
    / "migrate-notion.mjs"
)
TICK = (
    ROOT
    / "skills"
    / "growth-operations-xiaohongshu"
    / "linkedin-growth"
    / "scripts"
    / "tick.mjs"
)
GPT_IMAGE2 = (
    ROOT
    / "skills"
    / "multimodal-media"
    / "gpt-image2"
    / "scripts"
    / "gpt-image2.mjs"
)


def run_node(source: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "-e", source, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def assert_node_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"node exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_notion_cache_is_private_atomic_and_missing_cache_is_not_a_toctou(tmp_path: Path) -> None:
    module_url = MIGRATE_NOTION.as_uri()
    result = run_node(
        """
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import path from "node:path";
        import {
          readJsonCache,
          writePrivateJsonAtomic,
        } from %s;

        const root = process.argv[1];
        const cache = path.join(root, "private-cache", "rows.json");
        assert.equal(readJsonCache(cache), null);

        writePrivateJsonAtomic(cache, [{ id: 1 }]);
        assert.deepEqual(readJsonCache(cache), [{ id: 1 }]);
        assert.equal(fs.statSync(path.dirname(cache)).mode & 0o777, 0o700);
        assert.equal(fs.statSync(cache).mode & 0o777, 0o600);
        assert.deepEqual(
          fs.readdirSync(path.dirname(cache)).sort(),
          ["rows.json"],
          "atomic temp files must be cleaned up",
        );

        fs.writeFileSync(cache, "{broken", { mode: 0o600 });
        assert.throws(() => readJsonCache(cache), SyntaxError);
        """
        % json.dumps(module_url),
        str(tmp_path),
    )
    assert_node_ok(result)


def test_notion_fetch_uses_fixed_endpoint_and_rejects_redirects(tmp_path: Path) -> None:
    module_url = MIGRATE_NOTION.as_uri()
    result = run_node(
        """
        import assert from "node:assert/strict";
        import path from "node:path";
        import { fetchAll, NOTION_QUERY_URL } from %s;

        const calls = [];
        const fetchImpl = async (url, options) => {
          calls.push({ url, options });
          return {
            ok: true,
            status: 200,
            json: async () => ({
              object: "list",
              results: [],
              has_more: false,
              next_cursor: null,
            }),
          };
        };

        const rows = await fetchAll({
          cachePath: path.join(process.argv[1], "cache", "rows.json"),
          fetchImpl,
          refetch: true,
          token: "test-token",
        });
        assert.deepEqual(rows, []);
        assert.equal(
          NOTION_QUERY_URL,
          "https://api.notion.com/v1/databases/28665216-8ab0-809c-a8dc-d8fe366d0266/query",
        );
        assert.equal(calls.length, 1);
        assert.equal(calls[0].url, NOTION_QUERY_URL);
        assert.equal(calls[0].options.redirect, "error");
        """
        % json.dumps(module_url),
        str(tmp_path),
    )
    assert_node_ok(result)


def test_tick_sqlite_immediate_claim_allows_only_one_live_contender(
    tmp_path: Path,
) -> None:
    module_url = TICK.as_uri()
    result = run_node(
        """
        import assert from "node:assert/strict";
        import {
          claimSchedulerLock,
          releaseSchedulerLock,
        } from %s;

        function fakeSettingsDb() {
          const rows = new Map();
          let immediateCalls = 0;
          return {
            rows,
            get immediateCalls() { return immediateCalls; },
            transaction(fn) {
              return {
                immediate() {
                  immediateCalls += 1;
                  return fn();
                },
              };
            },
            prepare(sql) {
              if (sql.startsWith("SELECT value")) {
                return {
                  get(key) {
                    return rows.has(key) ? { value: rows.get(key) } : undefined;
                  },
                };
              }
              if (sql.startsWith("INSERT INTO settings")) {
                return {
                  run(key, value) {
                    rows.set(key, value);
                    return { changes: 1 };
                  },
                };
              }
              if (sql.startsWith("DELETE FROM settings")) {
                return {
                  run(key) {
                    const changes = rows.delete(key) ? 1 : 0;
                    return { changes };
                  },
                };
              }
              throw new Error(`unexpected SQL: ${sql}`);
            },
          };
        }

        const db = fakeSettingsDb();
        assert.equal(
          claimSchedulerLock(db, "alice", 101, "owner-token-a", () => false),
          true,
        );
        assert.equal(
          claimSchedulerLock(db, "alice", 202, "owner-token-b", (pid) => pid === 101),
          false,
          "the second contender must observe the first live owner in the same immediate transaction",
        );
        assert.equal(db.immediateCalls, 2);
        assert.deepEqual(
          JSON.parse(db.rows.get("scheduler_lock:alice")),
          { pid: 101, token: "owner-token-a" },
        );

        // A stale owner cannot delete a newer token.
        db.rows.set(
          "scheduler_lock:alice",
          JSON.stringify({ pid: 202, token: "owner-token-b" }),
        );
        assert.equal(releaseSchedulerLock(db, "alice", 101, "owner-token-a"), false);
        assert.deepEqual(
          JSON.parse(db.rows.get("scheduler_lock:alice")),
          { pid: 202, token: "owner-token-b" },
        );
        assert.equal(releaseSchedulerLock(db, "alice", 202, "owner-token-b"), true);
        assert.equal(db.rows.has("scheduler_lock:alice"), false);
        """
        % json.dumps(module_url),
        str(tmp_path),
    )
    assert_node_ok(result)


def test_tick_legacy_file_lock_is_read_only_and_unsafe_types_block(
    tmp_path: Path,
) -> None:
    module_url = TICK.as_uri()
    result = run_node(
        """
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import path from "node:path";
        import {
          legacyLockBlocks,
          readLegacyLockSnapshot,
        } from %s;

        const root = process.argv[1];
        const livePath = path.join(root, "live.lock");
        fs.writeFileSync(livePath, String(process.pid), { mode: 0o600 });
        const before = fs.readFileSync(livePath, "utf8");
        assert.equal(readLegacyLockSnapshot(livePath).record.pid, process.pid);
        assert.equal(legacyLockBlocks(livePath, (pid) => pid === process.pid), true);
        assert.equal(fs.readFileSync(livePath, "utf8"), before);

        const deadPath = path.join(root, "dead.lock");
        fs.writeFileSync(deadPath, "2147483647", { mode: 0o600 });
        assert.equal(legacyLockBlocks(deadPath, () => false), false);
        assert.equal(fs.existsSync(deadPath), true, "dead legacy locks are ignored, never deleted");

        const invalidPath = path.join(root, "invalid.lock");
        fs.writeFileSync(invalidPath, "not-a-lock", { mode: 0o600 });
        assert.equal(
          legacyLockBlocks(invalidPath, () => false),
          false,
          "a stable malformed regular file cannot belong to a live legacy worker",
        );
        assert.equal(fs.existsSync(invalidPath), true);

        const symlinkTarget = path.join(root, "symlink-target");
        const symlinkPath = path.join(root, "symlink.lock");
        fs.writeFileSync(symlinkTarget, String(process.pid), { mode: 0o600 });
        fs.symlinkSync(symlinkTarget, symlinkPath);
        assert.equal(
          legacyLockBlocks(symlinkPath, () => false),
          true,
          "unsafe legacy path types remain fail-closed",
        );
        assert.equal(fs.readFileSync(symlinkTarget, "utf8"), String(process.pid));
        """
        % json.dumps(module_url),
        str(tmp_path),
    )
    assert_node_ok(result)


def test_gpt_image_local_reads_and_downloads_validate_bytes_and_mime(tmp_path: Path) -> None:
    module_url = GPT_IMAGE2.as_uri()
    result = run_node(
        """
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import path from "node:path";
        import {
          downloadImageURL,
          loadReferenceImages,
        } from %s;

        const png = Buffer.from([
          0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
          0x00, 0x00, 0x00, 0x0d,
        ]);
        const root = process.argv[1];
        const validPath = path.join(root, "valid.png");
        const fakePath = path.join(root, "fake.png");
        fs.writeFileSync(validPath, png);
        fs.writeFileSync(fakePath, "not an image");

        const local = await loadReferenceImages([validPath]);
        assert.equal(local[0].mimeType, "image/png");
        assert.deepEqual(local[0].raw, png);
        await assert.rejects(
          loadReferenceImages([fakePath]),
          /格式不支持或无法识别/,
        );

        let requestOptions;
        globalThis.fetch = async (_url, options) => {
          requestOptions = options;
          return new Response(png, {
            status: 200,
            headers: {
              "content-type": "image/png",
              "content-length": String(png.length),
            },
          });
        };
        const downloaded = await downloadImageURL("https://images.example.test/a", 1000);
        assert.equal(requestOptions.redirect, "error");
        assert.equal(downloaded.mimeType, "image/png");
        assert.deepEqual(downloaded.raw, png);

        globalThis.fetch = async () => new Response(png, {
          status: 200,
          headers: { "content-type": "image/jpeg" },
        });
        await assert.rejects(
          downloadImageURL("https://images.example.test/mismatch", 1000),
          /content-type.*does not match/i,
        );

        globalThis.fetch = async () => new Response("not an image", {
          status: 200,
          headers: { "content-type": "image/png" },
        });
        await assert.rejects(
          downloadImageURL("https://images.example.test/fake", 1000),
          /format.*not supported|无法识别/i,
        );

        globalThis.fetch = async () => new Response(null, {
          status: 200,
          headers: {
            "content-type": "image/png",
            "content-length": String(10 * 1024 * 1024 + 1),
          },
        });
        await assert.rejects(
          downloadImageURL("https://images.example.test/large", 1000),
          /10MB/,
        );

        globalThis.fetch = async () => new Response(
          Buffer.alloc(10 * 1024 * 1024 + 1),
          {
            status: 200,
            headers: { "content-type": "image/png" },
          },
        );
        await assert.rejects(
          downloadImageURL("https://images.example.test/streamed-large", 1000),
          /10MB/,
        );
        """
        % json.dumps(module_url),
        str(tmp_path),
    )
    assert_node_ok(result)

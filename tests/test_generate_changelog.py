import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_changelog.py"


class GenerateChangelogTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def test_last_tag_uses_exclusive_tag_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Test")
            self.git(root, "config", "user.email", "test@example.com")
            (root / "before.txt").write_text("before\n", encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "feat: before tag")
            self.git(root, "tag", "v1.0.0")
            (root / "after.txt").write_text("after\n", encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "fix: after tag")

            result = subprocess.run(
                [
                    "python",
                    str(SCRIPT),
                    "--since",
                    "last-tag",
                    "--dry-run",
                ],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("Revision range: `v1.0.0..HEAD`", result.stdout)
            self.assertIn("after tag", result.stdout)
            self.assertNotIn("before tag", result.stdout)

    def test_explicit_ref_is_converted_to_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Test")
            self.git(root, "config", "user.email", "test@example.com")
            (root / "a").write_text("a", encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "chore: base")
            base = self.git(root, "rev-parse", "HEAD")
            (root / "b").write_text("b", encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "feat: current")

            result = subprocess.run(
                ["python", str(SCRIPT), "--since", base, "--dry-run"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn(f"Revision range: `{base}..HEAD`", result.stdout)
            self.assertIn("current", result.stdout)


if __name__ == "__main__":
    unittest.main()

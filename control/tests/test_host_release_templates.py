import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "control" / "host"
SHA_PATTERN = re.compile(r"\b[0-9a-f]{40}\b")


class HostReleaseTemplatesTest(unittest.TestCase):
    def test_release_json_is_the_only_hardcoded_release_sha(self):
        release = json.loads((HOST / "release.json").read_text(encoding="utf-8"))

        self.assertRegex(release["symphony_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(release["codex_version"], r"^\d+\.\d+\.\d+$")

        for path in HOST.glob("*"):
            if path.name == "release.json" or not path.is_file():
                continue
            self.assertEqual(SHA_PATTERN.findall(path.read_text(encoding="utf-8")), [], path.name)

    def test_both_launchers_and_compose_use_release_metadata(self):
        runtime = (HOST / "runtime-common.ps1").read_text(encoding="utf-8")
        owner = (HOST / "owner-common.ps1").read_text(encoding="utf-8")
        compose = (HOST / "docker-compose.release.yml").read_text(encoding="utf-8")

        self.assertIn("Get-SymphonyReleaseMetadata", runtime)
        self.assertIn("Get-SymphonyReleaseMetadata", owner)
        self.assertEqual(compose.count("${SYMPHONY_RELEASE_SHA:?"), 2)
        self.assertIn("${SYMPHONY_CODEX_VERSION:?", compose)

    def test_runtime_start_preflights_before_starting_any_service(self):
        start = (HOST / "runtime-start.ps1").read_text(encoding="utf-8")

        preflight = start.index("Assert-SymphonyReleasePreflight")
        owner_start = start.index("owner-control\\start.ps1")
        compose_up = start.index("docker compose")

        self.assertLess(preflight, owner_start)
        self.assertLess(preflight, compose_up)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required")
    def test_release_preflight_accepts_exact_clean_checkout_and_rejects_drift(self):
        helper = HOST / "release.ps1"

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "runtime"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
            ).stdout.strip()
            release_path = Path(temp_dir) / "release.json"
            release_path.write_text(
                json.dumps({"symphony_sha": sha, "codex_version": "0.153.4"}), encoding="utf-8"
            )

            clean = self._run_preflight(helper, release_path, repo)
            self.assertEqual(clean.returncode, 0, clean.stderr)

            (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            dirty = self._run_preflight(helper, release_path, repo)
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("local changes", dirty.stderr)

            subprocess.run(["git", "checkout", "--", "tracked.txt"], cwd=repo, check=True)
            release_path.write_text(
                json.dumps({"symphony_sha": "0" * 40, "codex_version": "0.153.4"}), encoding="utf-8"
            )
            mismatch = self._run_preflight(helper, release_path, repo)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("not pinned", mismatch.stderr)

    @staticmethod
    def _run_preflight(helper: Path, release_path: Path, repo: Path) -> subprocess.CompletedProcess[str]:
        command = (
            f". '{helper}'; "
            f"$release = Get-SymphonyReleaseMetadata -Path '{release_path}'; "
            f"Assert-SymphonyReleasePreflight -Release $release -RuntimeRoot '{repo}'"
        )
        return subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()

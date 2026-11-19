"""Unit tests for configuration, source validation and job persistence.

Run from the kdu-build-server directory:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from kdu_build_server.build_request import BuildRequestError, parse_build_request
from kdu_build_server.jobs import QUEUED, SUCCEEDED, JobStore
from kdu_build_server.platform_config import (
    PlatformConfigError,
    resolve_platform_config,
)
from kdu_build_server.settings import SourceSettings
from kdu_build_server.source import SourceError, SourceManager


class BuildRequestTests(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        request = parse_build_request(
            {"configuration": "Debug", "extra_properties": {"A": "1"}},
            "windows-x64",
            4,
        )
        self.assertEqual(request["platform"], "windows-x64")
        self.assertEqual(request["overrides"]["configuration"], "Debug")
        self.assertEqual(request["overrides"]["extra_properties"], {"A": "1"})

    def test_rejects_unknown_and_malformed_fields(self) -> None:
        with self.assertRaises(BuildRequestError):
            parse_build_request({"unknown": True}, "windows-x64", 4)
        with self.assertRaises(BuildRequestError):
            parse_build_request({"extra_properties": {"bad-key": "x"}}, "windows-x64", 4)
        with self.assertRaises(BuildRequestError):
            parse_build_request({"callback_url": "ftp://example.com"}, "windows-x64", 4)


class PlatformConfigTests(unittest.TestCase):
    def test_merge_order_and_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platforms = root / "platforms.json"
            platforms.write_text(
                json.dumps(
                    {
                        "default_platform": "base",
                        "defaults": {"configuration": "Release", "platform_toolset": "v145"},
                        "platforms": {
                            "base": {"msbuild_platform": "x64", "extra_properties": {"A": "1"}}
                        },
                    }
                ),
                encoding="utf-8",
            )
            provider = root / "provider.py"
            provider.write_text(
                "def get_platform_config(platform, context):\n"
                "    return {'platform_toolset': 'v143', 'extra_properties': {'B': '2'}}\n",
                encoding="utf-8",
            )
            config = resolve_platform_config(
                platform="base",
                platforms_file=platforms,
                provider=provider,
                overrides={"configuration": "Debug", "extra_properties": {"A": "3"}},
            )
            self.assertEqual(config["configuration"], "Debug")
            self.assertEqual(config["platform_toolset"], "v143")
            self.assertEqual(config["extra_properties"], {"A": "3", "B": "2"})

    def test_unknown_platform_without_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platforms = Path(directory) / "platforms.json"
            platforms.write_text(
                json.dumps({"platforms": {"x64": {}}}), encoding="utf-8"
            )
            with self.assertRaises(PlatformConfigError):
                resolve_platform_config("arm64", platforms_file=platforms)


class SourceManagerTests(unittest.TestCase):
    def make_manager(self, root: Path) -> SourceManager:
        settings = SourceSettings(
            repo="owner/repo",
            ref="master",
            github_token="",
            timeout_seconds=5,
            validate_files=("Source/KDU.sln",),
        )
        return SourceManager(settings, root, logging.getLogger("test-source"))

    def test_state_from_other_repository_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.make_manager(root)
            sha = "a" * 40
            (root / "source" / "releases" / sha).mkdir(parents=True)
            (root / "source" / "state.json").write_text(
                json.dumps({"repo": "other/repo", "ref": "master", "sha": sha}),
                encoding="utf-8",
            )
            self.assertIsNone(manager.read_state())

    def test_extraction_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.make_manager(root)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("repo/../evil.txt", "x")
            with self.assertRaises(SourceError):
                manager._extract(archive, root / "staging")


class JobStoreTests(unittest.TestCase):
    def test_lifecycle_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            try:
                job = store.create({"platform": "x64"}, "x64")
                self.assertEqual(job["status"], QUEUED)
                self.assertTrue(store.mark_running(job["id"]))
                store.mark_succeeded(
                    job["id"],
                    {"artifacts": []},
                    "a" * 40,
                    Path(directory) / "build.log",
                    Path(directory) / "artifact.zip",
                )
                self.assertEqual(store.get(job["id"])["status"], SUCCEEDED)
                self.assertEqual(len(store.list()), 1)
                self.assertEqual(store.prune(0), [job["id"]])
                self.assertIsNone(store.get(job["id"]))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

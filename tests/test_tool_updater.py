import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import app
import tool_updater


def build_wheel_bytes(files: dict[str, str]) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._stream = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ToolUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.previous_overlay = tool_updater.active_managed_python_root()

    def tearDown(self) -> None:
        if tool_updater.active_managed_python_root() != self.previous_overlay:
            app.reload_yt_dlp_runtime(self.previous_overlay)
        self.temp_dir.cleanup()

    def test_requires_python_and_ejs_parsing(self) -> None:
        self.assertTrue(tool_updater.python_satisfies_requires_python(">=3.11", (3, 12)))
        self.assertFalse(tool_updater.python_satisfies_requires_python(">=3.15", (3, 12)))
        self.assertTrue(tool_updater.python_satisfies_requires_python(">=3.10,<4.0", (3, 12)))
        self.assertFalse(tool_updater.python_satisfies_requires_python("<3.12", (3, 12)))

        requires_dist = [
            'requests<3,>=2.32.2; extra == "default"',
            'yt-dlp-ejs==0.8.0; extra == "default"',
            'yt-dlp-ejs==0.8.0; extra == "pin"',
        ]
        self.assertEqual(tool_updater.parse_required_ejs_version(requires_dist), "0.8.0")

    def test_meta_path_finder_beats_frozen_importer_and_metadata(self) -> None:
        overlay_dir = self.data_dir / "overlay"
        (overlay_dir / "yt_dlp").mkdir(parents=True)
        (overlay_dir / "yt_dlp" / "__init__.py").write_text("SENTINEL = 'overlay'\n", encoding="utf-8")
        (overlay_dir / "yt_dlp" / "version.py").write_text("__version__ = '2099.01.01'\n", encoding="utf-8")
        dist_info = overlay_dir / "yt_dlp-2099.01.01.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: yt-dlp\nVersion: 2099.01.01\n", encoding="utf-8")

        class FakeFrozenImporter(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "yt_dlp":
                    raise AssertionError("FrozenImporter should not intercept managed yt_dlp")
                return None

        fake_frozen = FakeFrozenImporter()
        sys.meta_path.insert(0, fake_frozen)
        try:
            tool_updater.purge_yt_dlp_modules()
            tool_updater.set_managed_python_overlay(overlay_dir)
            self.assertIs(sys.meta_path[0], tool_updater._MANAGED_PYTHON_FINDER)
            spec = importlib.util.find_spec("yt_dlp")
            self.assertIsNotNone(spec)
            self.assertEqual(Path(spec.origin).resolve(), (overlay_dir / "yt_dlp" / "__init__.py").resolve())
            imported = importlib.import_module("yt_dlp")
            self.assertEqual(getattr(imported, "SENTINEL", None), "overlay")
            self.assertEqual(importlib.metadata.version("yt-dlp"), "2099.01.01")
        finally:
            if fake_frozen in sys.meta_path:
                sys.meta_path.remove(fake_frozen)
            app.reload_yt_dlp_runtime(self.previous_overlay)

    def test_bootstrap_skips_stale_overlay_when_bundled_is_newer(self) -> None:
        stale_dir = self.data_dir / "managed_tools" / "python" / "yt-dlp-2025.01.01"
        (stale_dir / "yt_dlp").mkdir(parents=True)
        (stale_dir / "yt_dlp" / "__init__.py").write_text("", encoding="utf-8")
        (stale_dir / "yt_dlp" / "version.py").write_text("__version__ = '2025.01.01'\n", encoding="utf-8")
        tool_updater.save_managed_tools_state(
            {"yt_dlp": {"version": "2025.01.01", "path": str(stale_dir)}},
            data_dir=self.data_dir,
        )

        with patch.object(tool_updater, "detect_bundled_yt_dlp_version", return_value="2026.10.26"):
            activated = tool_updater.bootstrap_managed_python_packages(data_dir=self.data_dir)
        self.assertIsNone(activated)

    def test_hot_reload_and_rollback_of_yt_dlp_runtime(self) -> None:
        # Verify real reload + bgutil plugin re-registration works
        original_version = app.active_yt_dlp_version()
        reloaded_version = app.reload_yt_dlp_runtime(None)
        self.assertEqual(reloaded_version, original_version)
        self.assertTrue(app.ensure_po_token_provider_ready())

        # Verify broken overlay rolls back cleanly to working yt-dlp
        broken_dir = self.data_dir / "broken_overlay"
        (broken_dir / "yt_dlp").mkdir(parents=True)
        (broken_dir / "yt_dlp" / "__init__.py").write_text("raise RuntimeError('corrupt wheel')\n", encoding="utf-8")
        (broken_dir / "yt_dlp" / "version.py").write_text("__version__ = '2099.9.9'\n", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            app.reload_yt_dlp_runtime(broken_dir)

        self.assertEqual(app.active_yt_dlp_version(), original_version)
        self.assertIsNone(tool_updater.active_managed_python_root())
        self.assertTrue(app.ensure_po_token_provider_ready())

    def test_update_yt_dlp_downloads_verifies_extracts_and_respects_etag(self) -> None:
        yt_dlp_whl, yt_dlp_sha = build_wheel_bytes({
            "yt_dlp/__init__.py": "class YoutubeDL: pass\n",
            "yt_dlp/version.py": "__version__ = '2026.12.01'\n",
            "yt_dlp-2026.12.01.dist-info/METADATA": "Metadata-Version: 2.1\nName: yt-dlp\nVersion: 2026.12.01\n",
        })
        ejs_whl, ejs_sha = build_wheel_bytes({
            "yt_dlp_ejs/__init__.py": "VERSION = '0.9.0'\n",
            "yt_dlp_ejs-0.9.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: yt-dlp-ejs\nVersion: 0.9.0\n",
        })

        pypi_yt_dlp = json.dumps({
            "info": {
                "version": "2026.12.01",
                "requires_python": ">=3.11",
                "requires_dist": ['yt-dlp-ejs==0.9.0; extra == "default"'],
            },
            "urls": [{
                "packagetype": "bdist_wheel",
                "filename": "yt_dlp-2026.12.01-py3-none-any.whl",
                "url": "https://files.example/yt_dlp-2026.12.01-py3-none-any.whl",
                "digests": {"sha256": yt_dlp_sha},
            }],
        }).encode("utf-8")
        pypi_ejs = json.dumps({
            "info": {"version": "0.9.0"},
            "urls": [{
                "packagetype": "bdist_wheel",
                "filename": "yt_dlp_ejs-0.9.0-py3-none-any.whl",
                "url": "https://files.example/yt_dlp_ejs-0.9.0-py3-none-any.whl",
                "digests": {"sha256": ejs_sha},
            }],
        }).encode("utf-8")

        etag_seen = []

        def fake_urlopen(request, timeout=20, **kwargs):
            url = request.full_url
            if url == tool_updater.PYPI_YT_DLP_URL:
                if request.headers.get("If-none-match") == '"etag-v1"':
                    etag_seen.append(True)
                    raise HTTPError(url, 304, "Not Modified", {}, None)
                return FakeResponse(pypi_yt_dlp, headers={"ETag": '"etag-v1"'})
            if url == tool_updater.PYPI_YT_DLP_EJS_URL_TEMPLATE.format(version="0.9.0"):
                return FakeResponse(pypi_ejs)
            if url.endswith("yt_dlp-2026.12.01-py3-none-any.whl"):
                return FakeResponse(yt_dlp_whl)
            if url.endswith("yt_dlp_ejs-0.9.0-py3-none-any.whl"):
                return FakeResponse(ejs_whl)
            raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(tool_updater, "urlopen", side_effect=fake_urlopen):
            outcome = tool_updater.update_yt_dlp_if_needed(
                current_version="2026.10.26",
                current_ejs_version="0.8.0",
                user_agent="Test/1.0",
                force=True,
                data_dir=self.data_dir,
            )

        self.assertEqual(outcome.status, "updated")
        self.assertEqual(outcome.latest_version, "2026.12.01")
        installed_dir = Path(outcome.installed_path)
        self.assertTrue((installed_dir / "yt_dlp" / "version.py").is_file())
        self.assertTrue((installed_dir / "yt_dlp_ejs" / "__init__.py").is_file())

        # Within interval, automatic check should skip
        skipped = tool_updater.update_yt_dlp_if_needed(
            current_version="2026.12.01",
            current_ejs_version="0.9.0",
            user_agent="Test/1.0",
            force=False,
            data_dir=self.data_dir,
        )
        self.assertEqual(skipped.status, "skipped")

        # After interval expires, ETag 304 returns up_to_date without re-downloading
        state = tool_updater.load_managed_tools_state(self.data_dir)
        state["yt_dlp"]["last_checked_at"] = time.time() - 3600
        tool_updater.save_managed_tools_state(state, self.data_dir)
        with patch.object(tool_updater, "urlopen", side_effect=fake_urlopen):
            not_mod = tool_updater.update_yt_dlp_if_needed(
                current_version="2026.12.01",
                current_ejs_version="0.9.0",
                user_agent="Test/1.0",
                force=False,
                data_dir=self.data_dir,
            )
        self.assertEqual(not_mod.status, "up_to_date")
        self.assertEqual(etag_seen, [True])

    def test_update_yt_dlp_rejects_bad_sha256(self) -> None:
        yt_dlp_whl, _ = build_wheel_bytes({"yt_dlp/__init__.py": "", "yt_dlp/version.py": "__version__='2026.12.02'"})
        pypi_yt_dlp = json.dumps({
            "info": {"version": "2026.12.02", "requires_python": ">=3.11"},
            "urls": [{
                "packagetype": "bdist_wheel",
                "filename": "yt_dlp-2026.12.02-py3-none-any.whl",
                "url": "https://files.example/yt_dlp-2026.12.02-py3-none-any.whl",
                "digests": {"sha256": "0" * 64},
            }],
        }).encode("utf-8")

        def fake_urlopen(request, timeout=20, **kwargs):
            if request.full_url == tool_updater.PYPI_YT_DLP_URL:
                return FakeResponse(pypi_yt_dlp)
            return FakeResponse(yt_dlp_whl)

        with patch.object(tool_updater, "urlopen", side_effect=fake_urlopen):
            outcome = tool_updater.update_yt_dlp_if_needed(
                current_version="2026.10.26",
                current_ejs_version="0.8.0",
                user_agent="Test/1.0",
                force=True,
                data_dir=self.data_dir,
            )
        self.assertEqual(outcome.status, "error")
        self.assertIn("SHA-256", outcome.message)

    def test_safe_extract_zip_blocks_zipslip(self) -> None:
        bad_zip = self.data_dir / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("../escape.txt", "pwned")
        with self.assertRaises(RuntimeError):
            tool_updater.safe_extract_zip(bad_zip, self.data_dir / "extract")

    def test_twitch_downloader_update_and_resolution(self) -> None:
        binary_name = tool_updater.twitch_downloader_binary_name()
        asset_pattern = tool_updater.twitch_downloader_asset_pattern()
        self.assertIsNotNone(asset_pattern)

        if sys.platform.startswith("win"):
            asset_name = "TwitchDownloaderCLI-1.56.5-Windows-x64.zip"
        elif sys.platform == "darwin":
            asset_name = "TwitchDownloaderCLI-1.56.5-MacOSArm64.zip" if "arm" in tool_updater.platform.machine().lower() else "TwitchDownloaderCLI-1.56.5-MacOS-x64.zip"
        else:
            asset_name = "TwitchDownloaderCLI-1.56.5-LinuxArm64.zip" if "arm" in tool_updater.platform.machine().lower() or "aarch" in tool_updater.platform.machine().lower() else "TwitchDownloaderCLI-1.56.5-Linux-x64.zip"

        zip_bytes, zip_sha = build_wheel_bytes({f"nested/{binary_name}": "fake-twitch-binary-content"})
        release_json = json.dumps({
            "tag_name": "1.56.5",
            "assets": [{
                "name": asset_name,
                "browser_download_url": f"https://github.example/{asset_name}",
                "digest": f"sha256:{zip_sha}",
            }],
        }).encode("utf-8")

        bundled_bin = self.data_dir / "bundled" / binary_name
        bundled_bin.parent.mkdir(parents=True)
        bundled_bin.write_text("old-bundled-binary", encoding="utf-8")

        def fake_urlopen(request, timeout=20, **kwargs):
            if request.full_url == tool_updater.TWITCH_DOWNLOADER_LATEST_RELEASE_URL:
                return FakeResponse(release_json, headers={"ETag": '"td-etag"'})
            if request.full_url.endswith(asset_name):
                return FakeResponse(zip_bytes)
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        with (
            patch.object(tool_updater, "urlopen", side_effect=fake_urlopen),
            patch.object(tool_updater, "detect_twitch_downloader_version", return_value="1.56.4"),
        ):
            outcome = tool_updater.update_twitch_downloader_if_needed(
                bundled_candidates=[bundled_bin],
                user_agent="Test/1.0",
                force=True,
                data_dir=self.data_dir,
            )

            self.assertEqual(outcome.status, "updated")
            self.assertEqual(outcome.latest_version, "1.56.5")
            resolved = tool_updater.resolve_twitch_downloader_executable([bundled_bin], data_dir=self.data_dir)
            self.assertEqual(resolved, outcome.installed_path)

        # If a newer bundled version ships later, bundled wins over stale managed binary
        with patch.object(tool_updater, "detect_twitch_downloader_version", return_value="1.58.0"):
            resolved_newer_bundled = tool_updater.resolve_twitch_downloader_executable([bundled_bin], data_dir=self.data_dir)
            self.assertEqual(resolved_newer_bundled, str(bundled_bin))

    def test_detect_twitch_version_parses_nonzero_exit_code(self) -> None:
        fake_bin = self.data_dir / "TwitchDownloaderCLI"
        fake_bin.write_text("placeholder", encoding="utf-8")

        class FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "TwitchDownloaderCLI 1.56.4\nCopyright (c) 2026\n"

        with patch.object(tool_updater.subprocess, "run", return_value=FakeCompleted()):
            detected = tool_updater.detect_twitch_downloader_version(fake_bin, data_dir=self.data_dir)
        self.assertEqual(detected, "1.56.4")


if __name__ == "__main__":
    unittest.main()

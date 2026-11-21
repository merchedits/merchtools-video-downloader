import subprocess
import unittest
from unittest.mock import patch

import app


class DownloadRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.worker = app.DownloadWorker(
            "https://www.youtube.com/watch?v=BdcM47VGoHc", "clip.%(ext)s",
            "clip.mp4", "ffmpeg.exe", expected_duration=26,
            start_seconds=1070, end_seconds=1096,
        )

    def test_progress_does_not_swallow_diagnostics_containing_equals(self):
        logs = []
        self.worker.signals.log.connect(logs.append)
        self.worker.on_ffmpeg_output("[https] URL https://example.com/video?id=123 returned 403 Forbidden")
        self.worker.on_ffmpeg_output("out_time_ms=13000000")
        self.worker.on_ffmpeg_output("progress=continue")
        self.assertIn("403 Forbidden", logs[0])
        self.assertEqual(self.worker.ffmpeg_progress_percentage(), 50)
        self.assertNotIn("[https] URL https://example.com/video?id", self.worker.ffmpeg_progress_state)

    def test_ffmpeg_probe_keeps_requested_streams(self):
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append(kwargs)

        tracked = self.worker.make_tracking_popen(FakePopen)
        with patch.object(self.worker, "attach_ffmpeg_process") as attach:
            tracked(["ffmpeg.exe", "-bsfs"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            attach.assert_not_called()
        self.assertEqual(calls[0], {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT})

    def test_bundled_location_context_is_restored_after_download_error(self):
        original = app.FFmpegPostProcessor._ffmpeg_location.get()

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def download(inner, urls):
                self.assertEqual(app.FFmpegPostProcessor._ffmpeg_location.get(), "ffmpeg.exe")
                raise app.DownloadError("test failure")

        original_popen = app.yt_dlp_external.Popen
        with patch.object(app, "YoutubeDL", FakeYoutubeDL), patch.object(app, "yt_dlp_js_runtime_options", return_value={}), patch.object(app, "browser_cookie_candidates", return_value=[]):
            with self.assertRaises(app.DownloadError):
                self.worker.run_standard_download()
        self.assertEqual(app.FFmpegPostProcessor._ffmpeg_location.get(), original)
        self.assertIs(app.yt_dlp_external.Popen, original_popen)

    def test_hardware_encoder_failure_detection(self):
        self.assertTrue(
            app.is_hardware_encoder_failure(
                "ERROR: ffmpeg exited with code 1",
                ["[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: out of memory"],
                "h264_nvenc",
            )
        )
        self.assertFalse(
            app.is_hardware_encoder_failure(
                "ERROR: HTTP Error 403 Forbidden",
                ["[https] 403 Forbidden"],
                "h264_nvenc",
            )
        )
        self.assertFalse(
            app.is_hardware_encoder_failure(
                "ERROR: ffmpeg exited with code 1",
                ["Generic error"],
                None,
            )
        )

    def test_download_retries_with_cpu_when_hardware_encoder_fails(self):
        attempts = []
        finished = []
        errors = []
        self.worker.use_hardware_acceleration = True
        self.worker.signals.finished.connect(finished.append)
        self.worker.signals.error.connect(errors.append)

        def fake_standard_download():
            attempts.append(self.worker.use_hardware_acceleration)
            if self.worker.use_hardware_acceleration:
                self.worker.active_hardware_encoder = "h264_nvenc"
                self.worker.ffmpeg_error_lines.append("[h264_nvenc] Driver does not support the required nvenc API version")
                raise app.DownloadError("ERROR: ffmpeg exited with code 1")
            return 0

        with (
            patch.object(self.worker, "run_standard_download", side_effect=fake_standard_download),
            patch.dict(app.HARDWARE_ENCODER_CACHE, {"ffmpeg.exe": "h264_nvenc"}, clear=False),
        ):
            self.worker.run()
            self.assertIsNone(app.HARDWARE_ENCODER_CACHE.get("ffmpeg.exe"))

        self.assertEqual(attempts, [True, False])
        self.assertEqual(errors, [])
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0]["ok"])

    def test_trim_local_clip_retries_with_cpu_on_hardware_failure(self):
        from pathlib import Path
        import tempfile

        commands = []

        class FakeProc:
            def __init__(self, args, **kwargs):
                commands.append(args)
                self._failed = "h264_nvenc" in args
                self.stderr = iter(["[h264_nvenc] OpenEncodeSessionEx failed\n"] if self._failed else [])

            def wait(self):
                return 1 if self._failed else 0

        self.worker.use_hardware_acceleration = True
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            src.write_bytes(b"src")
            with (
                patch.object(app.subprocess, "Popen", side_effect=FakeProc),
                patch.dict(app.HARDWARE_ENCODER_CACHE, {"ffmpeg.exe": "h264_nvenc"}, clear=False),
            ):
                code = self.worker.trim_local_clip(src, out)
                self.assertIsNone(app.HARDWARE_ENCODER_CACHE.get("ffmpeg.exe"))

        self.assertEqual(code, 0)
        self.assertEqual(len(commands), 2)
        self.assertIn("h264_nvenc", commands[0])
        self.assertNotIn("h264_nvenc", commands[1])


if __name__ == "__main__":
    unittest.main()

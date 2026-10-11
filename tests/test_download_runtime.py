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


if __name__ == "__main__":
    unittest.main()

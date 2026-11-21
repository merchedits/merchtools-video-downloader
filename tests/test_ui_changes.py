import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app
import tool_updater


class MainWindowBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.user_data_patch = patch.object(app, "user_data_dir", return_value=self.data_dir)
        self.tool_data_patch = patch.object(tool_updater, "user_data_dir", return_value=self.data_dir)
        self.dep_patch = patch.object(app.MainWindow, "start_dependency_check", lambda self: None)
        self.user_data_patch.start()
        self.tool_data_patch.start()
        self.dep_patch.start()
        self.window = app.MainWindow()
        self.window.dependencies_ready = True
        self.window.ffmpeg_path = "ffmpeg"

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        self.qapp.processEvents()
        self.dep_patch.stop()
        self.tool_data_patch.stop()
        self.user_data_patch.stop()
        self.temp_dir.cleanup()

    def test_end_time_autofills_zero_start_time_and_clears_when_erased(self) -> None:
        self.assertEqual(self.window.start_input.text(), "")
        self.window.end_input.setText("0:10")
        self.assertEqual(self.window.start_input.text(), "0:00")

        # Clearing end time removes the autofilled 0:00
        self.window.end_input.clear()
        self.assertEqual(self.window.start_input.text(), "")

        # Editing start time manually prevents auto-clear
        self.window.end_input.setText("0:25")
        self.assertEqual(self.window.start_input.text(), "0:00")
        self.window.start_input.setText("0:05")
        self.window.on_start_time_edited("0:05")
        self.window.end_input.clear()
        self.assertEqual(self.window.start_input.text(), "0:05")

    def test_download_with_only_end_time_uses_zero_start_seconds(self) -> None:
        self.window.url_input.setText("https://www.youtube.com/watch?v=BdcM47VGoHc")
        self.window.output_dir_input.setText(str(self.data_dir / "out"))
        self.window.end_input.setText("0:10")
        # Even if user manually clears start_input after typing end_input, download_video fills 0:00
        self.window.start_input.clear()

        started_workers = []

        def fake_run_worker(worker, success_handler, error_handler, progress_handler=None):
            started_workers.append(worker)

        with (
            patch.object(self.window, "run_worker", side_effect=fake_run_worker),
            patch.object(self.window, "show_error") as show_error,
        ):
            self.window.download_video()

        show_error.assert_not_called()
        self.assertEqual(len(started_workers), 1)
        self.assertEqual(self.window.start_input.text(), "0:00")
        self.assertEqual(started_workers[0].start_seconds, 0)
        self.assertEqual(started_workers[0].end_seconds, 10)
        self.assertEqual(started_workers[0].expected_duration, 10)

    def test_download_finished_has_no_modal_popup_and_plays_sound(self) -> None:
        self.window.reveal_checkbox.setChecked(False)
        with (
            patch.object(app.QMessageBox, "information") as info_popup,
            patch.object(self.window, "play_download_complete_sound") as play_sound,
        ):
            self.window.on_download_finished({"ok": True})

        info_popup.assert_not_called()
        play_sound.assert_called_once()

    def test_download_cancelled_does_not_play_sound_or_popup(self) -> None:
        with (
            patch.object(app.QMessageBox, "information") as info_popup,
            patch.object(self.window, "play_download_complete_sound") as play_sound,
        ):
            self.window.on_download_finished({"cancelled": True})

        info_popup.assert_not_called()
        play_sound.assert_not_called()

    def test_kittycat_and_classic_ui_options_are_removed(self) -> None:
        self.assertFalse(hasattr(self.window, "kittycat_menu"))
        self.assertFalse(hasattr(self.window, "cat_checkbox"))
        self.assertFalse(hasattr(self.window, "classic_ui_checkbox"))
        self.assertFalse(hasattr(self.window, "cat_mode_enabled"))
        self.assertFalse(hasattr(self.window, "classic_ui_enabled"))
        self.assertFalse(hasattr(self.window.title_bar, "toggle_button"))

        self.window.persist_user_settings()
        saved = app.load_user_settings()
        self.assertNotIn("cat_mode", saved)
        self.assertNotIn("classic_ui", saved)
        self.assertIn("output_dir", saved)
        self.assertIn("audio_only_wav", saved)

    def test_tool_update_defers_yt_dlp_reload_while_downloading(self) -> None:
        self.window.is_downloading = True
        fake_root = self.data_dir / "managed_tools" / "python" / "yt-dlp-2099.01.01"
        outcome = tool_updater.ToolUpdateOutcome(
            tool="yt-dlp",
            status="updated",
            current_version="2026.10.26",
            latest_version="2099.01.01",
            installed_path=str(fake_root),
        )
        with (
            patch.object(app, "reload_yt_dlp_runtime", return_value="2099.01.01") as reload_fn,
            patch.object(self.window, "play_download_complete_sound"),
        ):
            self.window.on_tool_updates_finished({"outcomes": [outcome], "force": False, "silent": True})
            reload_fn.assert_not_called()
            self.assertTrue(self.window._pending_yt_dlp_reload)

            self.window.reveal_checkbox.setChecked(False)
            self.window.on_download_finished({"ok": True})
            reload_fn.assert_called_once_with(fake_root)
            self.assertFalse(self.window._pending_yt_dlp_reload)

    def test_manual_check_for_updates_also_triggers_forced_tool_update_check(self) -> None:
        with (
            patch.object(self.window, "check_for_tool_updates") as check_tools,
            patch.object(self.window, "run_update_worker"),
        ):
            self.window.check_for_updates(silent=False)
        check_tools.assert_called_once_with(force=True, silent=False)

    def test_clip_filename_includes_range_and_auto_increments_without_overwrite_popup(self) -> None:
        out_dir = self.data_dir / "clips"
        out_dir.mkdir(parents=True)
        self.window.url_input.setText("https://www.youtube.com/watch?v=BdcM47VGoHc")
        self.window.output_dir_input.setText(str(out_dir))
        self.window.video_title = "Cool Highlight"
        self.window.last_auto_filename = "Cool Highlight"
        self.window.filename_input.setText("Cool Highlight")
        self.window.end_input.setText("0:10")

        started_workers = []

        def fake_run_worker(worker, success_handler, error_handler, progress_handler=None):
            started_workers.append(worker)
            self.window.is_downloading = False

        with (
            patch.object(self.window, "run_worker", side_effect=fake_run_worker),
            patch.object(app.QMessageBox, "question") as overwrite_popup,
        ):
            self.window.download_video()
            first_path = Path(started_workers[-1].output_path)
            self.assertEqual(first_path.name, "Cool Highlight [00-00 - 00-10].mp4")

            # Simulate first clip saved on disk, then grab another range from same video
            first_path.write_bytes(b"clip1")
            self.window.start_input.setText("0:20")
            self.window.end_input.setText("0:35")
            self.window.download_video()
            second_path = Path(started_workers[-1].output_path)
            self.assertEqual(second_path.name, "Cool Highlight [00-20 - 00-35].mp4")

            # Downloading the exact same range again auto-increments instead of prompting to overwrite
            self.window.start_input.setText("0:00")
            self.window.end_input.setText("0:10")
            self.window.download_video()
            dup_path = Path(started_workers[-1].output_path)
            self.assertEqual(dup_path.name, "Cool Highlight [00-00 - 00-10] (2).mp4")

            # Custom filename is preserved as-is, and auto-increments on collision
            self.window.filename_input.setText("my_intro")
            self.window.download_video()
            custom_path = Path(started_workers[-1].output_path)
            self.assertEqual(custom_path.name, "my_intro.mp4")
            custom_path.write_bytes(b"intro")
            self.window.download_video()
            custom_dup_path = Path(started_workers[-1].output_path)
            self.assertEqual(custom_dup_path.name, "my_intro (2).mp4")

        overwrite_popup.assert_not_called()

    def test_reveal_in_explorer_selects_exact_file_on_windows(self) -> None:
        clip_file = self.data_dir / "My Clip [00-00 - 00-10].mp4"
        clip_file.write_bytes(b"video")
        self.window.last_output_path = clip_file

        with (
            patch.object(app, "IS_WINDOWS", True),
            patch.object(app.subprocess, "Popen") as popen,
        ):
            self.window.reveal_in_explorer()

        popen.assert_called_once_with(f'explorer.exe /select,"{clip_file.resolve()}"')

    def test_vendored_bgutil_is_2_0_2_and_provider_loads(self) -> None:
        import json

        server_dir = Path(app.__file__).resolve().parent / "tools" / "bgutil-ytdlp-pot-provider" / "server"
        pkg = json.loads((server_dir / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["version"], "2.0.2")
        self.assertTrue((server_dir / "build" / "generate_once.js").is_file())
        self.assertTrue(app.ensure_po_token_provider_ready())


if __name__ == "__main__":
    unittest.main()

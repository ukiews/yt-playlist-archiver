import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web import server


class ProgressLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Path(self.folder.name)
        self.config_patch = patch.object(server, "CONFIG_DIR", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        for name, path in (
            ("MANUAL_LOG", self.config / "web-manual-run.log"),
            ("LOCK_DIR", self.config / ".yt-playlist-archiver-schedule-lock"),
            ("LEGACY_LOCK_DIR", self.config / ".legacy-schedule-lock"),
        ):
            fixed_path = patch.object(server, name, path)
            fixed_path.start()
            self.addCleanup(fixed_path.stop)
        self.started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.job = {"id": "job-1", "kind": "manual", "label": "Run hymns_video", "startedAt": self.started, "running": True}
        self.job_patch = patch.object(server, "CURRENT_JOB", self.job)
        self.job_patch.start()
        self.addCleanup(self.job_patch.stop)
        cache_patch = patch.object(server, "PROGRESS_CACHE", None)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_live_item_uses_transfer_percent_not_playlist_position(self):
        (self.config / "web-manual-run.log").write_text(
            "[2020-01-01T00:00:00-05:00] Run hymns_video\n"
            "[ytdl-sub:downloader] Downloading entry 1/1: Old track\n"
            "[ytdl-sub:yt-dlp] [download] 100.0% of 10MiB\n"
            f"[{self.started}] Run hymns_video\n"
            "[ytdl-sub] Beginning subscription download for hymns_video\n"
            "[ytdl-sub:downloader] Downloading entry 2/5: New hymn\n"
        )
        result = server.progress_payload()
        self.assertEqual(result["entryCurrent"], 2)
        self.assertEqual(result["entryTotal"], 5)
        self.assertIsNone(result["percent"])
        self.assertEqual([item["title"] for item in result["activeItems"]], ["New hymn"])

        with (self.config / "web-manual-run.log").open("a") as log:
            log.write("[ytdl-sub:yt-dlp] [youtube] Extracting URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n")
            log.write("[ytdl-sub:yt-dlp] [download]  42.7% of 25.00MiB at 2.00MiB/s ETA 00:07\r")
        result = server.progress_payload()
        self.assertEqual(result["percent"], 42.7)
        self.assertEqual(result["activeItems"][0]["percent"], 42.7)
        self.assertEqual(result["activeItems"][0]["videoId"], "dQw4w9WgXcQ")
        self.assertIn("ETA 00:07", result["activeItems"][0]["detail"])

    def test_scheduled_run_ignores_previous_check(self):
        (self.config / "cron").write_text("#!/bin/bash\n")
        lock = self.config / ".yt-playlist-archiver-schedule-lock"
        lock.mkdir()
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        (self.config / ".cron.log").write_text(
            "2020-01-01 01:00:00 EST Running schedule group frequent (10-minute interval).\n"
            "[ytdl-sub:downloader] Downloading entry 1/1: Old track\n"
            f"{stamp} EDT Running schedule group frequent (10-minute interval).\n"
            "[ytdl-sub] Beginning subscription download for music_audio\n"
            "[ytdl-sub:downloader] Downloading entry 1/1: New song\n"
            "[ytdl-sub:yt-dlp] [download]  8.0% of 12.00MiB\n"
        )
        with patch.object(server, "CURRENT_JOB", None):
            result = server.progress_payload()
        self.assertEqual(result["percent"], 8.0)
        self.assertEqual([item["title"] for item in result["activeItems"]], ["New song"])

    def test_complete_transfer_changes_to_processing_without_stale_speed(self):
        log = self.config / "web-manual-run.log"
        log.write_text(
            f"[{self.started}] Run hymns_video\n"
            "[ytdl-sub] Beginning subscription download for hymns_video\n"
            "[ytdl-sub:downloader] Downloading entry 1/2: First hymn\n"
            "[ytdl-sub:yt-dlp] [download]  81.0% of 20MiB at 2.00MiB/s ETA 00:03\n"
            "[ytdl-sub:yt-dlp] [download] 100.0% of 20MiB at 2.00MiB/s ETA 00:00\n"
            "[ytdl-sub:yt-dlp] [Merger] Merging formats\n"
        )
        item = server.progress_payload()["activeItems"][0]
        self.assertEqual(item["phase"], "Processing downloaded media")
        self.assertEqual(item["detail"], "Transfer complete")
        self.assertEqual(item["percent"], 100.0)

        with log.open("a") as handle:
            handle.write("[ytdl-sub:downloader] Downloading entry 2/2: Second hymn\n")
        items = server.progress_payload()["activeItems"]
        self.assertEqual(items[0]["phase"], "Finishing check")
        self.assertEqual(items[1]["phase"], "Starting download")

    def test_unchanged_log_reuses_progress_parse(self):
        (self.config / "web-manual-run.log").write_text(
            f"[{self.started}] Run hymns_video\n"
            "[ytdl-sub] Beginning subscription download for hymns_video\n"
        )
        with patch.object(server, "active_log_lines", wraps=server.active_log_lines) as parse:
            server.progress_payload()
            server.progress_payload()
            self.assertEqual(parse.call_count, 1)
            with (self.config / "web-manual-run.log").open("a") as handle:
                handle.write("[ytdl-sub:downloader] Downloading entry 1/1: New hymn\n")
            server.progress_payload()
            self.assertEqual(parse.call_count, 2)

    def test_existing_scheduler_is_upgraded_once(self):
        cron = self.config / "cron"
        cron.write_text('#!/bin/bash\nif ytdl-sub sub --suppress-colors --log-level info --match "${active_subscriptions[@]}"; then\n  :\nfi\n')
        self.assertTrue(server.enable_download_progress_logs())
        self.assertIn("--log-level verbose", cron.read_text())
        self.assertFalse(server.enable_download_progress_logs())
        self.assertEqual(len(list((self.config / ".dashboard-backups").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from web import server


class MissingQueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = self.root / "config"
        self.media = self.root / "videos"
        self.config.mkdir()
        self.media.mkdir()
        paths = {
            "CONFIG_DIR": self.config,
            "LOCK_DIR": self.config / ".yt-playlist-archiver-schedule-lock",
            "LEGACY_LOCK_DIR": self.config / ".legacy-schedule-lock",
            "STATE_DIR": self.config / ".yt-playlist-archiver-schedule-state",
            "LEGACY_STATE_DIR": self.config / ".legacy-schedule-state",
            "HISTORY_FILE": self.config / "download-history.json",
            "MISSING_QUEUE_FILE": self.config / ".yt-playlist-archiver-missing-queue.json",
            "DASHBOARD_SETTINGS_FILE": self.config / "dashboard-settings.json",
            "MANUAL_LOG": self.config / "web-manual-run.log",
            "LAST_ERROR_FILE": self.config / "ytdl-sub-last-error.txt",
            "PAUSED_FILE": self.config / "paused-subscriptions.txt",
            "PAUSE_ALL_FILE": self.config / "all-subscriptions-paused",
        }
        for name, value in paths.items():
            current = patch.object(server, name, value)
            current.start()
            self.addCleanup(current.stop)
        current_job = patch.object(server, "CURRENT_JOB", None)
        current_job.start()
        self.addCleanup(current_job.stop)
        self.verify_impl = server.verify_missing_media_items
        verifier = patch.object(
            server,
            "verify_missing_media_items",
            side_effect=lambda items, _rows: [{**item, "availability": "available"} for item in items],
        )
        verifier.start()
        self.addCleanup(verifier.stop)

        (self.config / "config.yaml").write_text(
            "presets:\n"
            "  yt_downloader_video:\n"
            "    output_options:\n"
            "      output_directory: '{output_dir}'\n"
            "      file_name: '{title_sanitized}.{ext}'\n"
            "      download_archive_name: '.ytdl-sub-{subscription_name}-download-archive.json'\n"
            "      maintain_download_archive: true\n",
            encoding="utf-8",
        )
        (self.config / "subscriptions.yaml").write_text(
            "yt_downloader_video:\n"
            "  sample_video:\n"
            "    download: https://www.youtube.com/playlist?list=sample\n"
            "    overrides:\n"
            f"      output_dir: {self.media}\n",
            encoding="utf-8",
        )
        (self.config / "cron").write_text(
            "#!/bin/bash\nrun_if_due standard 30 sample_video || overall_status=1\n",
            encoding="utf-8",
        )
        self.archive = self.media / ".ytdl-sub-sample_video-download-archive.json"
        self.archive.write_text(json.dumps({
            "aaaaaaaaaaa": {"file_names": ["Missing Video.mp4"], "upload_date": "2026-01-01"},
            "bbbbbbbbbbb": {"file_names": ["Present Video.mp4"], "upload_date": "2026-01-02"},
        }), encoding="utf-8")
        (self.media / "Present Video.mp4").write_bytes(b"present")

    def test_scan_only_returns_archived_media_missing_from_disk(self):
        items = server.missing_media_items(server.subscription_rows())
        self.assertEqual([item["videoId"] for item in items], ["aaaaaaaaaaa"])
        self.assertEqual(items[0]["title"], "Missing Video")
        self.assertEqual(items[0]["subscriptionId"], "sample_video")

    def test_playlist_verification_removes_items_no_longer_at_source(self):
        items = server.missing_media_items(server.subscription_rows())
        removed = {**items[0], "videoId": "ccccccccccc", "key": "sample_video:ccccccccccc"}
        result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"entries": [{"id": "aaaaaaaaaaa", "title": "Current title", "channel": "Current channel"}]}),
        )
        with patch.object(server.subprocess, "run", return_value=result):
            verified = self.verify_impl([items[0], removed], server.subscription_rows())
        self.assertEqual([item["videoId"] for item in verified], ["aaaaaaaaaaa"])
        self.assertEqual(verified[0]["title"], "Current title")
        self.assertEqual(verified[0]["channel"], "Current channel")

    def test_queue_uses_configured_timeout_and_owns_scheduler_lock(self):
        (self.config / "dashboard-settings.json").write_text('{"missingQueueTimeoutMinutes": 21}')
        state = server.start_missing_queue()
        self.assertEqual(state["timeoutMinutes"], 21)
        self.assertEqual(len(state["items"]), 1)
        self.assertTrue(server.LOCK_DIR.is_dir())
        server.cancel_missing_queue()
        self.assertFalse(server.MISSING_QUEUE_FILE.exists())
        self.assertFalse(server.LOCK_DIR.exists())

    def test_recovery_uses_direct_selected_urls_and_merges_successful_archive(self):
        archive = json.loads(self.archive.read_text())
        archive["ccccccccccc"] = {"file_names": ["Another Missing Video.mp4"], "upload_date": "2026-01-03"}
        self.archive.write_text(json.dumps(archive))
        state = server.start_missing_queue()
        selected = next(item for item in state["items"] if item["videoId"] == "aaaaaaaaaaa")
        captured = {}

        def fake_start(kind, label, command, **kwargs):
            captured.update(kind=kind, label=label, command=command, **kwargs)
            server.CURRENT_JOB = {"id": "recovery", "kind": kind, "label": label, "running": True}
            return dict(server.CURRENT_JOB)

        with patch.object(server, "start_job", side_effect=fake_start):
            job = server.start_missing_recovery([selected["key"]])

        self.assertEqual(job["kind"], "missing-recovery")
        temporary_file = Path(server.missing_queue_state()["temporarySubscriptionFile"])
        temporary = yaml.safe_load(temporary_file.read_text())
        recovered = temporary["yt_downloader_video"]["sample_video"]
        self.assertEqual(recovered["download"], "https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertNotIn("ccccccccccc", temporary_file.read_text())
        self.assertIn("recovery", recovered["output_options"]["download_archive_name"])

        context = server.missing_queue_state()["recoveryContexts"][0]
        new_entry = {"file_names": ["Missing Video 1080p.mp4"], "upload_date": "2026-01-01"}
        Path(context["temporaryArchive"]).write_text(json.dumps({"aaaaaaaaaaa": new_entry}))
        captured["on_finish"](0)

        merged = json.loads(self.archive.read_text())
        self.assertEqual(merged["aaaaaaaaaaa"], new_entry)
        self.assertIn("bbbbbbbbbbb", merged)
        self.assertFalse(server.MISSING_QUEUE_FILE.exists())
        self.assertFalse(Path(context["temporaryArchive"]).exists())
        self.assertFalse(temporary_file.exists())


if __name__ == "__main__":
    unittest.main()

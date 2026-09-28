import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from web import server


class LinkedAudioTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Path(self.folder.name)
        for name, path in {
            "CONFIG_DIR": self.config,
            "DASHBOARD_SETTINGS_FILE": self.config / "dashboard-settings.json",
            "PAUSED_FILE": self.config / "paused-subscriptions.txt",
            "PAUSE_ALL_FILE": self.config / "all-subscriptions-paused",
            "STATE_DIR": self.config / ".schedule-state",
            "LEGACY_STATE_DIR": self.config / ".legacy-state",
        }.items():
            current = patch.object(server, name, path)
            current.start()
            self.addCleanup(current.stop)
        (self.config / "config.yaml").write_text(
            "presets:\n"
            "  yt_downloader_video:\n"
            "    format: video-default\n"
            "    output_options:\n"
            "      download_archive_name: '.ytdl-sub-{subscription_name}-download-archive.json'\n"
            "  yt_downloader_audio:\n"
            "    format: audio-default\n"
            "    embed_thumbnail: true\n"
            "    output_options:\n"
            "      download_archive_name: '.ytdl-sub-{subscription_name}-download-archive.json'\n",
            encoding="utf-8",
        )
        (self.config / "subscriptions.yaml").write_text(
            "yt_downloader_video: {}\nyt_downloader_audio: {}\n", encoding="utf-8"
        )
        (self.config / "cron").write_text(
            "#!/bin/bash\nrun_if_due standard 30 || overall_status=1\n", encoding="utf-8"
        )

    @staticmethod
    def payload(**updates):
        result = {
            "id": "sample_video",
            "mode": "video",
            "url": "https://www.youtube.com/playlist?list=sample",
            "outputDir": "/media/videos/Sample",
            "audioOutputDir": "/media/music/Sample",
            "additionalAudio": True,
            "scheduleGroup": "standard",
            "intervalMinutes": 30,
            "genre": "Documentary",
            "format": "",
            "metadata": {"title": "{title}", "artist": "{uploader}"},
        }
        result.update(updates)
        return result

    def test_create_adds_linked_audio_job_and_one_logical_subscription(self):
        server.create_subscription(self.payload())

        data = yaml.safe_load((self.config / "subscriptions.yaml").read_text())
        self.assertIn("sample_video", data["yt_downloader_video"])
        self.assertIn("sample_audio", data["yt_downloader_audio"])
        audio = data["yt_downloader_audio"]["sample_audio"]
        self.assertEqual(audio["overrides"]["output_dir"], "/media/music/Sample")
        self.assertNotIn("format", audio)
        self.assertEqual(audio["music_tags"]["genres"], ["Documentary"])
        self.assertIn("sample_video sample_audio", (self.config / "cron").read_text())
        self.assertEqual(server.linked_audio_outputs(), {"sample_video": "sample_audio"})

        rows = server.logical_subscription_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rawIds"], ["sample_video", "sample_audio"])
        self.assertTrue(rows[0]["additionalAudio"])

    def test_audio_destination_defaults_to_main_destination(self):
        server.create_subscription(self.payload(audioOutputDir=""))
        data = yaml.safe_load((self.config / "subscriptions.yaml").read_text())
        self.assertEqual(
            data["yt_downloader_audio"]["sample_audio"]["overrides"]["output_dir"],
            "/media/videos/Sample",
        )

    def test_pause_and_remove_apply_to_both_jobs(self):
        server.create_subscription(self.payload())
        server.set_subscription_paused("sample_video", True)
        self.assertEqual(server.paused_subscriptions(), {"sample_video", "sample_audio"})
        server.set_subscription_paused("sample_video", False)
        self.assertEqual(server.paused_subscriptions(), set())

        server.remove_subscription("sample_video")
        data = yaml.safe_load((self.config / "subscriptions.yaml").read_text())
        self.assertNotIn("sample_video", data["yt_downloader_video"])
        self.assertNotIn("sample_audio", data["yt_downloader_audio"])
        self.assertNotIn("sample_video", (self.config / "cron").read_text())
        self.assertNotIn("sample_audio", (self.config / "cron").read_text())
        self.assertEqual(server.linked_audio_outputs(), {})

    def test_disable_and_reenable_reuses_deterministic_audio_id(self):
        server.create_subscription(self.payload())
        archive = Path("/media/music/Sample/.ytdl-sub-sample_audio-download-archive.json")
        # Archive storage is outside subscriptions.yaml and is never removed by this operation.
        server.patch_subscription("sample_video", self.payload(additionalAudio=False))
        data = yaml.safe_load((self.config / "subscriptions.yaml").read_text())
        self.assertNotIn("sample_audio", data["yt_downloader_audio"])
        self.assertEqual(server.linked_audio_outputs(), {})

        server.patch_subscription("sample_video", self.payload(additionalAudio=True))
        data = yaml.safe_load((self.config / "subscriptions.yaml").read_text())
        self.assertIn("sample_audio", data["yt_downloader_audio"])
        self.assertEqual(server.linked_audio_outputs(), {"sample_video": "sample_audio"})
        self.assertEqual(server.subscription_archive_path(next(
            row for row in server.subscription_rows() if row["id"] == "sample_audio"
        )), archive)

    def test_settings_show_one_schedule_member_for_linked_outputs(self):
        server.create_subscription(self.payload())
        standard = next(item for item in server.settings_payload()["schedules"] if item["id"] == "standard")
        self.assertEqual(standard["subscriptions"], ["sample_video"])
        self.assertEqual(standard["label"], "Sample Video")

    def test_existing_unlinked_video_and_audio_remain_separate(self):
        server.create_subscription(self.payload(additionalAudio=False))
        server.create_subscription(self.payload(
            id="sample_audio", mode="audio", outputDir="/media/music/Sample", additionalAudio=False
        ))
        rows = server.logical_subscription_rows()
        self.assertEqual({row["id"] for row in rows}, {"sample_video", "sample_audio"})
        self.assertFalse(next(row for row in rows if row["id"] == "sample_video")["additionalAudio"])

    def test_enabling_audio_links_compatible_existing_subscription_without_rewriting_it(self):
        server.create_subscription(self.payload(additionalAudio=False))
        server.create_subscription(self.payload(
            id="sample_audio",
            mode="audio",
            outputDir="/media/music/Existing",
            additionalAudio=False,
            format="bestaudio[ext=m4a]",
            genre="Existing genre",
            metadata={"title": "{title}", "artist": "Existing artist"},
        ))
        before = yaml.safe_load((self.config / "subscriptions.yaml").read_text())["yt_downloader_audio"]["sample_audio"]

        with self.assertRaises(server.ConfirmationRequired) as requested:
            server.patch_subscription("sample_video", self.payload(additionalAudio=True))
        self.assertEqual(requested.exception.confirmation["type"], "link-existing-audio")
        self.assertIn("Existing files, archives, destinations", requested.exception.confirmation["message"])
        self.assertEqual(server.linked_audio_outputs(), {})

        server.patch_subscription(
            "sample_video",
            self.payload(additionalAudio=True, confirmLinkExistingAudio=True),
        )

        after = yaml.safe_load((self.config / "subscriptions.yaml").read_text())["yt_downloader_audio"]["sample_audio"]
        self.assertEqual(after, before)
        self.assertEqual(server.linked_audio_outputs(), {"sample_video": "sample_audio"})
        rows = server.logical_subscription_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audioOutputDir"], "/media/music/Existing")

    def test_enabling_audio_refuses_existing_subscription_with_different_source(self):
        server.create_subscription(self.payload(additionalAudio=False))
        server.create_subscription(self.payload(
            id="sample_audio",
            mode="audio",
            url="https://www.youtube.com/playlist?list=another",
            outputDir="/media/music/Other",
            additionalAudio=False,
        ))
        with self.assertRaisesRegex(ValueError, "different YouTube source"):
            server.patch_subscription("sample_video", self.payload(additionalAudio=True))
        self.assertEqual(server.linked_audio_outputs(), {})

    def test_activity_groups_linked_outputs_by_youtube_video(self):
        (self.config / "dashboard-settings.json").write_text(json.dumps({
            "linkedAudioOutputs": {"sample_video": "sample_audio"}
        }))
        rows = [
            {"id": "sample_video", "name": "Sample", "genre": "Documentary"},
            {"id": "sample_audio", "name": "Sample", "genre": "Documentary"},
        ]
        downloads = [
            {"id": "v", "subscriptionId": "sample_video", "videoId": "abcdefghijk", "title": "Episode",
             "mode": "video", "fileName": "Episode.mp4", "folder": "/media/videos/Sample", "size": 100,
             "downloadedAt": "2026-09-28T10:00:00-04:00", "channel": "Creator"},
            {"id": "a", "subscriptionId": "sample_audio", "videoId": "abcdefghijk", "title": "Episode",
             "mode": "audio", "fileName": "Episode.m4a", "folder": "/media/music/Sample", "size": 20,
             "downloadedAt": "2026-09-28T10:01:00-04:00", "channel": "Creator"},
        ]
        grouped = server.grouped_downloads(downloads, rows)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["modes"], ["video", "audio"])
        self.assertEqual([item["extension"] for item in grouped[0]["outputs"]], ["MP4", "M4A"])
        self.assertEqual(grouped[0]["size"], 120)


if __name__ == "__main__":
    unittest.main()

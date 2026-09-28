#!/usr/bin/env python3
"""Small LAN dashboard for managing ytdl-sub."""

from __future__ import annotations

import datetime as dt
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import yaml


CONFIG_DIR = Path(os.environ.get("YTDL_SUB_CONFIG_DIR", "/config"))
APP_DIR = Path(__file__).resolve().parent
APP_VERSION = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))
PASSWORD_FILE = Path(os.environ.get("DASHBOARD_PASSWORD_FILE", CONFIG_DIR / "dashboard-password"))
SECRET_FILE = Path(os.environ.get("DASHBOARD_SECRET_FILE", CONFIG_DIR / "dashboard-secret"))
LOCK_DIR = CONFIG_DIR / ".yt-playlist-archiver-schedule-lock"
STATE_DIR = CONFIG_DIR / ".yt-playlist-archiver-schedule-state"
LEGACY_PREFIX = "media" + "human"
LEGACY_LOCK_DIR = CONFIG_DIR / f".{LEGACY_PREFIX}-schedule-lock"
LEGACY_STATE_DIR = CONFIG_DIR / f".{LEGACY_PREFIX}-schedule-state"
MANUAL_LOG = CONFIG_DIR / "web-manual-run.log"
LAST_ERROR_FILE = CONFIG_DIR / "ytdl-sub-last-error.txt"
PAUSED_FILE = CONFIG_DIR / "paused-subscriptions.txt"
PAUSE_ALL_FILE = CONFIG_DIR / "all-subscriptions-paused"
HISTORY_FILE = CONFIG_DIR / "download-history.json"
MISSING_QUEUE_FILE = CONFIG_DIR / ".yt-playlist-archiver-missing-queue.json"
DASHBOARD_SETTINGS_FILE = CONFIG_DIR / "dashboard-settings.json"
FIREFOX_PROFILE_DIR = Path(os.environ.get("FIREFOX_PROFILE_DIR", "/firefox-profile"))
FIREFOX_BROWSER_PORT = int(os.environ.get("FIREFOX_BROWSER_PORT", "0"))
FIREFOX_BROWSER_URL = os.environ.get("FIREFOX_BROWSER_URL", "").strip()
FIREFOX_CONTAINER_NAME = os.environ.get("FIREFOX_CONTAINER_NAME", "").strip()
DOCKER_SOCKET = Path(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))
ALLOWED_FILES = {"config.yaml", "subscriptions.yaml", "cron"}
MAX_BODY = 256 * 1024
DEFAULT_FILES = {
    "config.yaml": 0o600,
    "subscriptions.yaml": 0o600,
    "cron": 0o755,
    "youtube-auth-check.sh": 0o755,
}
VIDEO_PRESETS = ("yt_downloader_video", LEGACY_PREFIX + "_video")
AUDIO_PRESETS = ("yt_downloader_audio", LEGACY_PREFIX + "_audio")
PRESET_MODES = tuple((name, "video") for name in VIDEO_PRESETS) + tuple((name, "audio") for name in AUDIO_PRESETS)

DISPLAY_NAMES = {
    "watch_later_video": "Watch Later",
    "kids_video": "Для Дітей",
    "hymns_video": "Hymns",
    "hymns_audio": "Hymns",
    "music_video": "Music",
    "music_audio": "Music",
    "christmas_video": "Christmas Music",
    "christmas_audio": "Christmas Music",
}
JOB_LOCK = threading.Lock()
HISTORY_LOCK = threading.Lock()
PROGRESS_LOCK = threading.Lock()
CURRENT_JOB: dict | None = None
PROGRESS_CACHE: tuple[tuple, dict] | None = None
MEDIA_EXTENSIONS = {".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"}
DEFAULT_MISSING_QUEUE_TIMEOUT = 15


class ConfirmationRequired(Exception):
    def __init__(self, message: str, confirmation: dict):
        super().__init__(message)
        self.confirmation = confirmation


def read_text(path: Path, limit: int | None = None) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[-limit:] if limit else data
    except OSError:
        return ""


def ensure_default_files() -> list[str]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for name, mode in DEFAULT_FILES.items():
        destination = CONFIG_DIR / name
        if destination.exists():
            continue
        source = APP_DIR / "defaults" / name
        atomic_write(destination, source.read_text(encoding="utf-8"), mode)
        created.append(name)
    return created


def enable_download_progress_logs() -> bool:
    """Upgrade the dashboard's original scheduler command without changing custom scripts."""
    path = CONFIG_DIR / "cron"
    original = read_text(path)
    old = 'ytdl-sub sub --suppress-colors --log-level info --match "${active_subscriptions[@]}"'
    if old not in original:
        return False
    updated = original.replace(old, old.replace("--log-level info", "--log-level verbose"))
    validate_file("cron", updated)
    safe_backup(path)
    atomic_write(path, updated, 0o755)
    return True


def schedule_lock_dir() -> Path:
    cron = read_text(CONFIG_DIR / "cron")
    return LEGACY_LOCK_DIR if LEGACY_LOCK_DIR.name in cron else LOCK_DIR


def schedule_state_dir() -> Path:
    cron = read_text(CONFIG_DIR / "cron")
    return LEGACY_STATE_DIR if LEGACY_STATE_DIR.name in cron else STATE_DIR


def preset_names(mode: str) -> tuple[str, ...]:
    return VIDEO_PRESETS if mode == "video" else AUDIO_PRESETS


def selected_preset(data: dict, mode: str) -> str:
    return next((name for name in preset_names(mode) if name in data), preset_names(mode)[0])


def archive_file_name(template: str, subscription_id: str) -> str:
    return (template or ".ytdl-sub-{subscription_name}-download-archive.json").replace(
        "{subscription_name}", subscription_id
    ).replace("{subscription_name_sanitized}", subscription_id)


def read_secret(path: Path) -> str:
    return read_text(path).strip()


def configured_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "").strip() or read_secret(PASSWORD_FILE)


def configured_secret() -> str:
    explicit = os.environ.get("DASHBOARD_SECRET", "").strip() or read_secret(SECRET_FILE)
    if explicit:
        return explicit
    return hashlib.sha256(f"ytdl-sub-dashboard:{configured_password()}".encode()).hexdigest()


def iso_time(timestamp: float | int | None) -> str | None:
    if not timestamp:
        return None
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def parse_timestamp(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def load_subscriptions() -> dict:
    with (CONFIG_DIR / "subscriptions.yaml").open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("subscriptions.yaml must contain a mapping")
    return data


def load_config() -> dict:
    with (CONFIG_DIR / "config.yaml").open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a mapping")
    return data


def paused_subscriptions() -> set[str]:
    return {
        line.strip()
        for line in read_text(PAUSED_FILE).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def all_paused() -> bool:
    return PAUSE_ALL_FILE.exists()


def set_all_paused(paused: bool) -> None:
    if paused:
        atomic_write(PAUSE_ALL_FILE, "paused\n", 0o600)
    else:
        PAUSE_ALL_FILE.unlink(missing_ok=True)


def set_subscription_paused(subscription_id: str, paused: bool) -> None:
    known = {row["id"] for row in subscription_rows()}
    if subscription_id not in known:
        raise ValueError("Unknown subscription")
    linked_ids = linked_subscription_ids(subscription_id)
    paused_ids = paused_subscriptions()
    if paused:
        paused_ids.update(linked_ids)
    else:
        paused_ids.difference_update(linked_ids)
    atomic_write(PAUSED_FILE, "".join(f"{name}\n" for name in sorted(paused_ids)), 0o600)


def schedule_definitions() -> dict[str, dict]:
    cron = read_text(CONFIG_DIR / "cron")
    definitions = {}
    pattern = re.compile(r"^run_if_due[ \t]+(\S+)[ \t]+(\d+)(?:[ \t]+([^|#]*?))?[ \t]*(?:\|\|.*)?$", re.MULTILINE)
    for match in pattern.finditer(cron):
        group, interval, members = match.groups()
        subscriptions = (members or "").strip().split()
        display_names = []
        for subscription in subscriptions:
            display_name = DISPLAY_NAMES.get(subscription, subscription.replace("_", " ").title())
            if display_name not in display_names:
                display_names.append(display_name)
        if not display_names:
            label = group.replace("-", " ").title()
        elif len(display_names) == 1:
            label = display_names[0]
        elif len(display_names) == 2:
            label = " & ".join(display_names)
        else:
            label = f"{', '.join(display_names[:-1])} & {display_names[-1]}"
        definitions[group] = {
            "id": group,
            "label": label,
            "intervalMinutes": int(interval),
            "subscriptions": subscriptions,
        }
    return definitions


def subscription_rows() -> list[dict]:
    data = load_subscriptions()
    config = load_config()
    schedules = schedule_definitions()
    paused_ids = paused_subscriptions()
    rows = []
    now = int(time.time())
    for preset, mode in PRESET_MODES:
        group = data.get(preset, {})
        if not isinstance(group, dict):
            continue
        for name, raw in group.items():
            item = raw if isinstance(raw, dict) else {}
            output_dir = str((item.get("overrides") or {}).get("output_dir", ""))
            preset_config = (config.get("presets") or {}).get(preset) or {}
            preset_output = preset_config.get("output_options") or {}
            item_output = item.get("output_options") or {}
            if not isinstance(preset_output, dict):
                preset_output = {}
            if not isinstance(item_output, dict):
                item_output = {}
            archive_template = str(item_output.get("download_archive_name") or preset_output.get("download_archive_name") or "")
            archive_path = Path(output_dir) / archive_file_name(archive_template, name)
            archive_count = 0
            archive_updated = None
            try:
                archive = json.loads(archive_path.read_text(encoding="utf-8"))
                archive_count = len(archive) if isinstance(archive, (dict, list)) else 0
                archive_updated = iso_time(archive_path.stat().st_mtime)
            except (OSError, ValueError):
                pass
            genre = None
            if mode == "video":
                tags = item.get("video_tags") or {}
                genre = tags.get("genre")
            else:
                tags = item.get("music_tags") or {}
                genres = tags.get("genres") or []
                genre = genres[0] if isinstance(genres, list) and genres else None
            schedule_group = next(
                (group_id for group_id, schedule in schedules.items() if name in schedule["subscriptions"]),
                "",
            )
            interval = schedules.get(schedule_group, {}).get("intervalMinutes", 0)
            last_success = parse_timestamp(schedule_state_dir() / f"{schedule_group}.last-success")
            next_run = last_success + interval * 60 if last_success and interval else 0
            rows.append({
                "id": name,
                "name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                "mode": mode,
                "url": str(item.get("download", "")),
                "outputDir": output_dir,
                "genre": genre,
                "metadata": {
                    "title": tags.get("title", ""),
                    "artist": tags.get("artist", ""),
                    "album": tags.get("album", ""),
                    "albumArtist": tags.get("albumartist", ""),
                    "date": tags.get("date", ""),
                },
                "embedThumbnail": bool(item.get("embed_thumbnail", preset_config.get("embed_thumbnail", mode == "audio"))),
                "format": str(item.get("format", "") or "Preset default"),
                "intervalMinutes": interval,
                "scheduleGroup": schedule_group,
                "lastSuccess": iso_time(last_success),
                "nextRun": iso_time(next_run if next_run > now else now),
                "archiveCount": archive_count,
                "archiveUpdated": archive_updated,
                "archivePath": str(archive_path),
                "authenticated": name == "watch_later_video",
                "paused": name in paused_ids or all_paused(),
                "manuallyPaused": name in paused_ids,
            })
    return rows


def load_dashboard_settings() -> dict:
    try:
        settings = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    return settings if isinstance(settings, dict) else {}


def save_dashboard_settings(settings: dict) -> None:
    if DASHBOARD_SETTINGS_FILE.exists():
        safe_backup(DASHBOARD_SETTINGS_FILE)
    atomic_write(DASHBOARD_SETTINGS_FILE, json.dumps(settings, indent=2, sort_keys=True) + "\n", 0o600)


def linked_audio_outputs() -> dict[str, str]:
    raw = load_dashboard_settings().get("linkedAudioOutputs") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(primary): str(audio)
        for primary, audio in raw.items()
        if re.fullmatch(r"[a-z][a-z0-9_]{2,49}", str(primary))
        and re.fullmatch(r"[a-z][a-z0-9_]{2,49}", str(audio))
    }


def update_linked_audio_outputs(mapping: dict[str, str]) -> None:
    settings = load_dashboard_settings()
    if mapping:
        settings["linkedAudioOutputs"] = dict(sorted(mapping.items()))
    else:
        settings.pop("linkedAudioOutputs", None)
    save_dashboard_settings(settings)


def linked_subscription_ids(subscription_id: str) -> list[str]:
    mapping = linked_audio_outputs()
    if subscription_id in mapping:
        return [subscription_id, mapping[subscription_id]]
    primary = next((key for key, value in mapping.items() if value == subscription_id), None)
    return [primary, subscription_id] if primary else [subscription_id]


def logical_subscription_rows(rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else subscription_rows()
    by_id = {row["id"]: row for row in rows}
    mapping = linked_audio_outputs()
    companion_ids = {audio for primary, audio in mapping.items() if primary in by_id and audio in by_id}
    logical = []
    for source in rows:
        if source["id"] in companion_ids:
            continue
        row = copy.deepcopy(source)
        audio_id = mapping.get(row["id"])
        audio = by_id.get(audio_id) if audio_id else None
        row["additionalAudio"] = bool(audio and row["mode"] == "video")
        row["rawIds"] = [row["id"]]
        row["modes"] = [row["mode"]]
        if row["additionalAudio"]:
            row["videoArchiveCount"] = row["archiveCount"]
            row["rawIds"].append(audio["id"])
            row["modes"].append("audio")
            row["audioSubscriptionId"] = audio["id"]
            row["audioOutputDir"] = audio["outputDir"]
            row["audioArchiveCount"] = audio["archiveCount"]
            row["audioArchiveUpdated"] = audio["archiveUpdated"]
            row["archiveCount"] = max(row["archiveCount"], audio["archiveCount"])
            row["manuallyPaused"] = row["manuallyPaused"] or audio["manuallyPaused"]
            row["paused"] = row["paused"] or audio["paused"]
        logical.append(row)
    return logical


def archive_entries(rows: list[dict]) -> dict[str, dict]:
    entries = {}
    for row in rows:
        archive_path = subscription_archive_path(row)
        try:
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(archive, dict):
            continue
        for video_id, raw in archive.items():
            item = raw if isinstance(raw, dict) else {}
            for file_name in item.get("file_names") or []:
                if not isinstance(file_name, str) or not file_name:
                    continue
                path = str(Path(row["outputDir"]) / file_name)
                key = f"{row['id']}:{video_id}:{file_name}"
                entries[key] = {
                    "id": key, "videoId": video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)) else "",
                    "title": Path(file_name).stem, "fileName": file_name, "playlist": row["name"],
                    "subscriptionId": row["id"], "mode": row["mode"], "genre": row["genre"],
                    "folder": row["outputDir"], "path": path,
                    "uploadDate": item.get("upload_date"),
                }
    return entries


def embedded_channel(path: Path) -> str:
    if path.suffix.lower() not in {".m4a", ".mp3", ".opus", ".mp4", ".mkv", ".webm"}:
        return ""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=artist,album_artist", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        tags = (json.loads(result.stdout).get("format") or {}).get("tags") or {}
        return str(tags.get("artist") or tags.get("album_artist") or "").strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def log_download_events(entries: dict[str, dict]) -> list[dict]:
    by_path = {(item["subscriptionId"], item["path"]): item for item in entries.values()}
    events = {}
    for log_path in (CONFIG_DIR / ".cron.log", MANUAL_LOG):
        current_time = None
        subscription = None
        created = False
        folder = None
        for line in read_text(log_path, 2_000_000).splitlines():
            stamp = re.search(r"(20\d\d-\d\d-\d\d(?:T| )\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d)?)", line)
            if stamp and ("Running " in line or line.startswith("[")):
                try:
                    current_time = dt.datetime.fromisoformat(stamp.group(1)).astimezone().timestamp()
                except ValueError:
                    pass
            match = re.search(r"Transaction log for\s+([^:]+):", line)
            if match:
                subscription = match.group(1).strip()
                created = False
                folder = None
                continue
            if line.strip() == "Files created:":
                created = True
                continue
            if line.strip().startswith("Files modified:") or line.strip().startswith("Files deleted:"):
                created = False
                continue
            if not created or not subscription:
                continue
            stripped = line.strip()
            if stripped.startswith("/media/"):
                folder = stripped
            elif folder and line.startswith("  ") and not line.startswith("    "):
                item = by_path.get((subscription, str(Path(folder) / stripped)))
                if item and current_time:
                    events[item["id"]] = {**item, "downloadedAt": iso_time(current_time)}
    return list(events.values())


def latest_downloads(rows: list[dict], limit: int = 80) -> list[dict]:
    entries = archive_entries(rows)
    with HISTORY_LOCK:
        try:
            ledger = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = None
        if not isinstance(ledger, dict):
            ledger = {"seen": list(entries), "events": log_download_events(entries)}
            changed = True
        else:
            changed = False
        seen = set(ledger.get("seen") or [])
        events = {item["id"]: item for item in ledger.get("events") or [] if isinstance(item, dict) and item.get("id")}
        for key, item in entries.items():
            if key not in seen:
                events[key] = {**item, "downloadedAt": iso_time(time.time())}
                seen.add(key)
                changed = True
        for event in log_download_events(entries):
            if events.get(event["id"], {}).get("downloadedAt") != event["downloadedAt"]:
                events[event["id"]] = event
                changed = True
        downloads = []
        for event in events.values():
            path = Path(event.get("path", ""))
            if not path.is_file():
                continue
            item = dict(event)
            item["size"] = path.stat().st_size
            video_id = item.get("videoId", "")
            item["thumbnail"] = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else None
            item["sourceUrl"] = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
            downloads.append(item)
        downloads.sort(key=lambda item: item.get("downloadedAt") or "", reverse=True)
        for item in downloads[:limit]:
            if "channel" not in item:
                item["channel"] = embedded_channel(Path(item["path"]))
                events[item["id"]]["channel"] = item["channel"]
                changed = True
        if changed:
            atomic_write(HISTORY_FILE, json.dumps({"seen": sorted(seen), "events": list(events.values())[-1000:]}, ensure_ascii=False), 0o600)
        return downloads[:limit]


def grouped_downloads(downloads: list[dict], rows: list[dict], limit: int = 80) -> list[dict]:
    mapping = linked_audio_outputs()
    reverse = {audio: primary for primary, audio in mapping.items()}
    rows_by_id = {row["id"]: row for row in rows}
    grouped: dict[str, dict] = {}
    for item in downloads:
        logical_id = reverse.get(item.get("subscriptionId"), item.get("subscriptionId"))
        media_key = item.get("videoId") or item.get("title") or item.get("id")
        key = f"{logical_id}:{media_key}"
        output = {
            "id": item.get("id"),
            "subscriptionId": item.get("subscriptionId"),
            "mode": item.get("mode"),
            "extension": Path(item.get("fileName", "")).suffix.lstrip(".").upper(),
            "fileName": item.get("fileName"),
            "folder": item.get("folder"),
            "path": item.get("path"),
            "size": item.get("size", 0),
            "downloadedAt": item.get("downloadedAt"),
        }
        if key not in grouped:
            primary = rows_by_id.get(logical_id) or rows_by_id.get(item.get("subscriptionId")) or {}
            grouped[key] = {
                **item,
                "id": key,
                "subscriptionId": logical_id,
                "playlist": primary.get("name") or item.get("playlist"),
                "genre": primary.get("genre") or item.get("genre"),
                "outputs": [],
                "modes": [],
                "size": 0,
            }
        entry = grouped[key]
        entry["outputs"].append(output)
        if output["mode"] not in entry["modes"]:
            entry["modes"].append(output["mode"])
        entry["size"] += output["size"] or 0
        if item.get("channel") and not entry.get("channel"):
            entry["channel"] = item["channel"]
        if (item.get("downloadedAt") or "") > (entry.get("downloadedAt") or ""):
            entry["downloadedAt"] = item["downloadedAt"]
    result = list(grouped.values())
    for item in result:
        item["outputs"].sort(key=lambda output: (output["mode"] != "video", output["fileName"] or ""))
        item["mode"] = item["modes"][0] if len(item["modes"]) == 1 else "both"
        item["fileName"] = " · ".join(output["fileName"] or "" for output in item["outputs"])
        folders = list(dict.fromkeys(output["folder"] for output in item["outputs"] if output["folder"]))
        item["folder"] = folders[0] if len(folders) == 1 else "Video and audio destinations"
    result.sort(key=lambda item: item.get("downloadedAt") or "", reverse=True)
    return result[:limit]


def dashboard_settings() -> dict:
    settings = load_dashboard_settings()
    timeout = settings.get("missingQueueTimeoutMinutes", DEFAULT_MISSING_QUEUE_TIMEOUT)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_MISSING_QUEUE_TIMEOUT
    return {
        "missingQueueTimeoutMinutes": min(120, max(5, timeout)),
        "linkedAudioOutputs": linked_audio_outputs(),
    }


def subscription_archive_path(row: dict) -> Path:
    return Path(row.get("archivePath") or (Path(row["outputDir"]) / f".ytdl-sub-{row['id']}-download-archive.json"))


def missing_media_items(rows: list[dict]) -> list[dict]:
    history = {}
    try:
        ledger = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        for event in ledger.get("events") or []:
            if isinstance(event, dict) and event.get("subscriptionId") and event.get("videoId"):
                history[(event["subscriptionId"], event["videoId"])] = event
    except (OSError, ValueError, AttributeError):
        pass

    items = []
    for row in rows:
        archive_path = subscription_archive_path(row)
        try:
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(archive, dict):
            continue
        for raw_video_id, archive_entry in archive.items():
            video_id = str(raw_video_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not isinstance(archive_entry, dict):
                continue
            media_names = [
                name for name in archive_entry.get("file_names") or []
                if isinstance(name, str) and Path(name).suffix.lower() in MEDIA_EXTENSIONS
            ]
            if not media_names:
                continue
            media_paths = [Path(row["outputDir"]) / name for name in media_names]
            if any(path.is_file() for path in media_paths):
                continue
            previous = history.get((row["id"], video_id), {})
            file_name = media_names[0]
            items.append({
                "key": f"{row['id']}:{video_id}",
                "videoId": video_id,
                "subscriptionId": row["id"],
                "playlist": row["name"],
                "mode": row["mode"],
                "genre": row.get("genre"),
                "title": previous.get("title") or Path(file_name).stem,
                "channel": previous.get("channel") or "",
                "fileName": file_name,
                "missingFiles": media_names,
                "folder": row["outputDir"],
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                "sourceUrl": f"https://www.youtube.com/watch?v={video_id}",
                "archivePath": str(archive_path),
            })
    return sorted(items, key=lambda item: (item["playlist"].lower(), item["title"].lower(), item["mode"]))


def verify_missing_media_items(items: list[dict], rows: list[dict]) -> list[dict]:
    rows_by_id = {row["id"]: row for row in rows}
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["subscriptionId"], []).append(item)
    verified = []
    cookie_file = CONFIG_DIR / "youtube_cookies.txt"
    for subscription_id, candidates in grouped.items():
        row = rows_by_id.get(subscription_id, {})
        source_url = str(row.get("url", "")).strip()
        command = ["yt-dlp", "--flat-playlist", "--dump-single-json", "--skip-download", "--ignore-errors"]
        if cookie_file.is_file() and cookie_file.stat().st_size:
            command.extend(["--cookies", str(cookie_file)])
        command.append(source_url)
        try:
            result = subprocess.run(
                command,
                cwd=CONFIG_DIR,
                text=True,
                capture_output=True,
                timeout=300,
                env={**os.environ, "NO_COLOR": "1"},
            )
            metadata = json.loads(result.stdout)
            if result.returncode or not isinstance(metadata, dict):
                raise ValueError("playlist metadata could not be read")
            entries = metadata.get("entries")
            if not isinstance(entries, list):
                entries = [metadata]
            available = {
                str(entry.get("id")): entry for entry in entries
                if isinstance(entry, dict) and entry.get("id")
            }
            for item in candidates:
                entry = available.get(item["videoId"])
                if not entry:
                    continue
                updated = dict(item)
                updated["title"] = str(entry.get("title") or item["title"])
                updated["channel"] = str(entry.get("channel") or entry.get("uploader") or item["channel"])
                updated["availability"] = "available"
                verified.append(updated)
        except (OSError, ValueError, subprocess.SubprocessError):
            for item in candidates:
                verified.append({**item, "availability": "unverified"})
    return sorted(verified, key=lambda item: (item["playlist"].lower(), item["title"].lower(), item["mode"]))


def write_missing_queue(state: dict) -> None:
    atomic_write(MISSING_QUEUE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n", 0o600)


def cleanup_missing_queue_artifacts(state: dict) -> None:
    temporary_file = state.get("temporarySubscriptionFile")
    if isinstance(temporary_file, str) and temporary_file.startswith(str(CONFIG_DIR) + os.sep):
        Path(temporary_file).unlink(missing_ok=True)
    for context in state.get("recoveryContexts") or []:
        temporary_archive = context.get("temporaryArchive") if isinstance(context, dict) else None
        if isinstance(temporary_archive, str):
            Path(temporary_archive).unlink(missing_ok=True)


def merge_missing_recovery_archives(state: dict) -> None:
    for context in state.get("recoveryContexts") or []:
        if not isinstance(context, dict):
            continue
        temporary_archive = Path(str(context.get("temporaryArchive", "")))
        original_archive = Path(str(context.get("originalArchive", "")))
        try:
            recovered_archive = json.loads(temporary_archive.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recovered_archive = {}
        if not isinstance(recovered_archive, dict):
            continue
        video_ids = context.get("videoIds") or []
        successful = {video_id: recovered_archive[video_id] for video_id in video_ids if video_id in recovered_archive}
        if not successful:
            continue
        try:
            original = json.loads(original_archive.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            original = {}
        if not isinstance(original, dict):
            original = {}
        if original_archive.exists():
            safe_backup(original_archive)
        original.update(successful)
        atomic_write(original_archive, json.dumps(original, ensure_ascii=False, indent=2) + "\n", 0o600)


def clear_missing_queue(state: dict | None = None) -> None:
    if state:
        cleanup_missing_queue_artifacts(state)
    MISSING_QUEUE_FILE.unlink(missing_ok=True)
    try:
        schedule_lock_dir().rmdir()
    except OSError:
        pass


def missing_queue_state() -> dict | None:
    try:
        state = json.loads(MISSING_QUEUE_FILE.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        clear_missing_queue()
        return None
    if not isinstance(state, dict) or not isinstance(state.get("items"), list):
        clear_missing_queue(state if isinstance(state, dict) else None)
        return None
    recovery_running = bool(CURRENT_JOB and CURRENT_JOB.get("kind") == "missing-recovery")
    if state.get("status") == "downloading" and not recovery_running:
        merge_missing_recovery_archives(state)
        clear_missing_queue(state)
        return None
    try:
        expires_at = float(state.get("expiresEpoch", 0))
    except (TypeError, ValueError):
        expires_at = 0
    if state.get("status") != "downloading" and time.time() >= expires_at:
        clear_missing_queue(state)
        return None
    if not schedule_lock_dir().exists():
        try:
            schedule_lock_dir().mkdir()
        except FileExistsError:
            pass
    return state


def start_missing_queue() -> dict | None:
    with JOB_LOCK:
        if CURRENT_JOB or schedule_lock_dir().exists():
            raise RuntimeError("Wait for the current download check to finish")
        schedule_lock_dir().mkdir()
    try:
        rows = subscription_rows()
        items = verify_missing_media_items(missing_media_items(rows), rows)
        if not items:
            clear_missing_queue()
            return None
        timeout = dashboard_settings()["missingQueueTimeoutMinutes"]
        now = time.time()
        state = {
            "id": secrets.token_hex(8),
            "status": "reviewing",
            "createdAt": iso_time(now),
            "expiresAt": iso_time(now + timeout * 60),
            "expiresEpoch": now + timeout * 60,
            "timeoutMinutes": timeout,
            "items": items,
        }
        write_missing_queue(state)
        return state
    except Exception:
        clear_missing_queue()
        raise


def cancel_missing_queue() -> None:
    state = missing_queue_state()
    if not state:
        return
    if CURRENT_JOB and CURRENT_JOB.get("kind") == "missing-recovery":
        raise RuntimeError("Wait for the selected downloads to finish")
    clear_missing_queue(state)


def browser_reachable(url: str) -> bool | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=0.8):
            return True
    except (OSError, ValueError):
        return False


def docker_request(method: str, path: str) -> tuple[int, dict]:
    if not FIREFOX_CONTAINER_NAME:
        raise RuntimeError("Firefox container control is not configured")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", FIREFOX_CONTAINER_NAME):
        raise RuntimeError("Firefox container name is invalid")
    if not DOCKER_SOCKET.exists():
        raise RuntimeError("Docker socket is not connected to this dashboard")
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Accept: application/json\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(12)
            client.connect(str(DOCKER_SOCKET))
            client.sendall(request)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except (OSError, TimeoutError) as exc:
        raise RuntimeError(f"Cannot reach Docker: {exc}") from exc
    raw = b"".join(chunks)
    headers, _, body = raw.partition(b"\r\n\r\n")
    if b"transfer-encoding: chunked" in headers.lower():
        decoded = bytearray()
        remaining = body
        while remaining:
            size_line, separator, remaining = remaining.partition(b"\r\n")
            if not separator:
                raise RuntimeError("Docker returned an invalid chunked response")
            try:
                size = int(size_line.split(b";", 1)[0], 16)
            except ValueError as exc:
                raise RuntimeError("Docker returned an invalid chunk size") from exc
            if size == 0:
                break
            if len(remaining) < size + 2:
                raise RuntimeError("Docker returned an incomplete response")
            decoded.extend(remaining[:size])
            remaining = remaining[size + 2:]
        body = bytes(decoded)
    status_line = headers.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("Docker returned an invalid response") from exc
    parsed = {}
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {}
    if status >= 400:
        raise RuntimeError(parsed.get("message") or f"Docker request failed ({status})")
    return status, parsed


def firefox_container_state() -> dict:
    if not FIREFOX_CONTAINER_NAME:
        return {"available": False, "running": None, "error": None}
    try:
        _, data = docker_request("GET", f"/containers/{quote(FIREFOX_CONTAINER_NAME, safe='')}/json")
        return {
            "available": True,
            "running": bool(data.get("State", {}).get("Running")),
            "error": None,
        }
    except RuntimeError as exc:
        return {"available": False, "running": None, "error": str(exc)}


def set_firefox_container(running: bool) -> dict:
    action = "start" if running else "stop"
    suffix = "" if running else "?t=10"
    docker_request("POST", f"/containers/{quote(FIREFOX_CONTAINER_NAME, safe='')}/{action}{suffix}")
    return firefox_container_state()


def auth_state() -> dict:
    marker = CONFIG_DIR / "AUTHENTICATION_REQUIRED"
    status_file = CONFIG_DIR / "youtube-auth-status.txt"
    cookie_file = CONFIG_DIR / "youtube_cookies.txt"
    container = firefox_container_state()
    return {
        "ok": cookie_file.is_file() and cookie_file.stat().st_size > 0 and not marker.exists(),
        "status": read_text(status_file, 1000).strip() or "Authentication has not been checked yet.",
        "checkedAt": iso_time(status_file.stat().st_mtime) if status_file.exists() else None,
        "cookieUpdatedAt": iso_time(cookie_file.stat().st_mtime) if cookie_file.exists() else None,
        "firefoxProfileAvailable": (FIREFOX_PROFILE_DIR / "profile" / "cookies.sqlite").is_file(),
        "firefoxBrowserPort": FIREFOX_BROWSER_PORT,
        "firefoxBrowserUrl": FIREFOX_BROWSER_URL,
        "firefoxBrowserAvailable": browser_reachable(FIREFOX_BROWSER_URL),
        "firefoxContainerControlAvailable": container["available"],
        "firefoxContainerRunning": container["running"],
        "firefoxContainerControlError": container["error"],
        "firefoxUpdatedAt": iso_time((FIREFOX_PROFILE_DIR / "profile" / "cookies.sqlite").stat().st_mtime)
        if (FIREFOX_PROFILE_DIR / "profile" / "cookies.sqlite").is_file() else None,
    }


def last_error_state() -> dict | None:
    candidates = [LAST_ERROR_FILE, CONFIG_DIR / "youtube-auth-last-error.log"]
    existing = [path for path in candidates if path.is_file() and path.stat().st_size]
    if not existing:
        return None
    path = max(existing, key=lambda item: item.stat().st_mtime)
    lines = [line.strip() for line in read_text(path, 12000).splitlines() if line.strip()]
    message = lines[-1] if lines else "A download task failed."
    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
    return {"message": message[:300], "at": iso_time(path.stat().st_mtime)}


def firefox_cookie_text() -> tuple[str, int]:
    source = FIREFOX_PROFILE_DIR / "profile" / "cookies.sqlite"
    if not source.is_file():
        raise ValueError("The persistent Firefox profile is not available to the dashboard")
    temporary_dir = Path("/tmp") / f"firefox-cookies-{secrets.token_hex(5)}"
    temporary_dir.mkdir(mode=0o700)
    copied = temporary_dir / "cookies.sqlite"
    try:
        shutil.copy2(source, copied)
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{source}{suffix}")
            if companion.is_file():
                shutil.copy2(companion, Path(f"{copied}{suffix}"))
        with sqlite3.connect(copied) as connection:
            rows = connection.execute(
                "SELECT host, path, isSecure, expiry, name, value, isHttpOnly "
                "FROM moz_cookies WHERE host = 'youtube.com' OR host LIKE '%.youtube.com'"
            ).fetchall()
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
    if not rows:
        raise ValueError("Firefox has no YouTube session yet. Sign in to YouTube in Firefox first")
    output = ["# Netscape HTTP Cookie File", "# Imported from the persistent Firefox profile.", ""]
    for host, path, secure, expiry, name, value, http_only in sorted(rows):
        cookie_host = f"#HttpOnly_{host}" if http_only else host
        include_subdomains = "TRUE" if host.startswith(".") else "FALSE"
        output.append("\t".join((
            cookie_host,
            include_subdomains,
            path or "/",
            "TRUE" if secure else "FALSE",
            str(max(0, int(expiry or 0))),
            name,
            value,
        )))
    return "\n".join(output) + "\n", len(rows)


def install_youtube_cookies(content: str) -> dict:
    global CURRENT_JOB
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if not first_line.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValueError("The selected file is not a Netscape cookies.txt file")
    if "\x00" in content:
        raise ValueError("The cookie file contains invalid data")
    with JOB_LOCK:
        if CURRENT_JOB or schedule_lock_dir().exists():
            raise RuntimeError("Wait for the current download check to finish before changing sign-in")
        schedule_lock_dir().mkdir()
    cookie_file = CONFIG_DIR / "youtube_cookies.txt"
    previous = cookie_file.read_bytes() if cookie_file.is_file() else None
    try:
        atomic_write(cookie_file, content, 0o600)
        result = subprocess.run(
            ["/config/youtube-auth-check.sh"],
            cwd=CONFIG_DIR,
            text=True,
            capture_output=True,
            timeout=120,
            env={**os.environ, "NO_COLOR": "1"},
        )
        with MANUAL_LOG.open("a", encoding="utf-8") as handle:
            stamp = iso_time(time.time())
            handle.write(f"\n[{stamp}] Import and verify YouTube sign-in\n")
            handle.write((result.stdout or result.stderr or "No validation output")[-4000:] + "\n")
        if result.returncode:
            if previous is None:
                cookie_file.unlink(missing_ok=True)
            else:
                tmp = cookie_file.with_name(f".{cookie_file.name}.restore-{secrets.token_hex(3)}")
                tmp.write_bytes(previous)
                os.chmod(tmp, 0o600)
                os.replace(tmp, cookie_file)
                subprocess.run(["/config/youtube-auth-check.sh"], cwd=CONFIG_DIR, capture_output=True, timeout=120)
            raise ValueError("YouTube rejected that session. The previous working sign-in was restored")
        LAST_ERROR_FILE.unlink(missing_ok=True)
        return auth_state()
    finally:
        try:
            schedule_lock_dir().rmdir()
        except OSError:
            pass


def disk_state(path: str) -> dict:
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def job_state() -> dict | None:
    with JOB_LOCK:
        if CURRENT_JOB:
            return dict(CURRENT_JOB)
    if MISSING_QUEUE_FILE.exists():
        return None
    if schedule_lock_dir().exists():
        try:
            started_at = iso_time(schedule_lock_dir().stat().st_mtime)
        except OSError:
            started_at = None
        return {"kind": "scheduled", "label": "Scheduled check", "startedAt": started_at, "running": True}
    return None


def active_log_lines(job: dict) -> list[str]:
    log_path = CONFIG_DIR / ".cron.log" if job.get("kind") == "scheduled" else MANUAL_LOG
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 2_000_000))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).replace("\r", "\n")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    started_at = job.get("startedAt")
    if not started_at:
        return []
    if job.get("kind") == "scheduled":
        start_time = dt.datetime.fromisoformat(started_at).timestamp()
        for index, line in enumerate(lines):
            match = re.match(r"^(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)\b.*Running schedule group", line)
            if match and dt.datetime.fromisoformat(match.group(1)).astimezone().timestamp() >= start_time - 5:
                return lines[index:]
    else:
        marker = f"[{started_at}] {job.get('label', '')}"
        for index in range(len(lines) - 1, -1, -1):
            if marker in lines[index]:
                return lines[index:]
    return []


def progress_payload() -> dict:
    global PROGRESS_CACHE
    job = job_state()
    if not job:
        return {"job": None, "activeItems": []}

    log_path = CONFIG_DIR / ".cron.log" if job.get("kind") == "scheduled" else MANUAL_LOG
    try:
        log_stat = log_path.stat()
        log_version = (log_stat.st_mtime_ns, log_stat.st_size)
    except OSError:
        log_version = (None, None)
    cache_key = (job.get("id"), job.get("kind"), job.get("startedAt"), log_path, *log_version)
    with PROGRESS_LOCK:
        if PROGRESS_CACHE and PROGRESS_CACHE[0] == cache_key:
            return PROGRESS_CACHE[1]
        lines = active_log_lines(job)

    subscription = ""
    title = ""
    phase = "Starting download check"
    detail = str(job.get("label") or "Download job")
    percent = None
    entry_current = None
    entry_total = None
    active_items = []
    current_item = None

    for line in lines:
        match = re.search(r"Beginning subscription download for\s+([^\s]+)", line)
        if match:
            subscription = match.group(1)
            phase = "Checking playlist"
            title = DISPLAY_NAMES.get(subscription, subscription.replace("_", " ").title())
            current_item = None
            percent = None
            entry_current = entry_total = None

        match = re.search(r"Downloading metadata for\s+(.+)$", line)
        if match and current_item is None:
            title = match.group(1).strip()
            phase = "Reading YouTube metadata"

        match = re.search(r"Downloading entry\s+(\d+)\s*/\s*(\d+):\s*(.+)$", line)
        if match:
            if current_item and current_item["percent"] == 100.0:
                current_item["phase"] = "Finishing check"
                current_item["detail"] = "Transfer complete"
            entry_current, entry_total = int(match.group(1)), int(match.group(2))
            title = match.group(3).strip()
            phase = "Downloading media"
            percent = None
            current_item = {
                "id": f"{job['startedAt']}:{subscription}:{entry_current}:{title}",
                "title": title,
                "subscription": subscription,
                "phase": "Starting download",
                "percent": None,
                "detail": "Waiting for transfer progress",
            }
            active_items.append(current_item)

        match = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", line)
        if match and current_item is not None:
            current_item["videoId"] = match.group(1)

        match = re.search(r"\[download\].*?([0-9]+(?:\.[0-9]+)?)%", line)
        if match and current_item is not None:
            percent = min(100.0, float(match.group(1)))
            phase = "Finishing transfer" if percent == 100.0 else "Downloading media"
            current_item["phase"] = phase
            current_item["percent"] = percent
            speed = re.search(r"at\s+([^\s]+/s)", line)
            eta = re.search(r"ETA\s+([^\s]+)", line)
            extras = []
            if speed:
                extras.append(speed.group(1))
            if eta:
                extras.append(f"ETA {eta.group(1)}")
            current_item["detail"] = "Transfer complete" if percent == 100.0 else "Current transfer" + (f" · {' · '.join(extras)}" if extras else "")

        if "Post-process" in line or "ExtractAudio" in line or "Merger" in line:
            phase = "Processing downloaded media"
            if current_item:
                current_item["phase"] = phase
                current_item["detail"] = "Transfer complete"
        if "Writing metadata" in line or "Metadata" in line and "Adding metadata" in line:
            phase = "Writing media tags"
            if current_item:
                current_item["phase"] = phase
                current_item["detail"] = "Transfer complete"
        if "Transaction log for" in line:
            phase = "Saving download history"
            if current_item:
                current_item["phase"] = "Saving download history"
                current_item["detail"] = "Finalizing download"
                current_item["percent"] = 100.0 if current_item["percent"] is not None else None
                current_item = None

    if subscription:
        playlist = DISPLAY_NAMES.get(subscription, subscription.replace("_", " ").title())
        mode = "Audio" if subscription.endswith("_audio") else "Video"
        detail = f"{playlist} · {mode}" + (f" · item {entry_current} of {entry_total}" if entry_total else "")
    if current_item and current_item["detail"] != "Waiting for transfer progress":
        detail = current_item["detail"]

    result = {
        "job": job,
        "title": title or str(job.get("label") or "Checking subscriptions"),
        "phase": phase,
        "detail": detail,
        "percent": percent,
        "subscription": subscription or None,
        "entryCurrent": entry_current,
        "entryTotal": entry_total,
        "activeItems": active_items[-20:],
    }
    with PROGRESS_LOCK:
        PROGRESS_CACHE = (cache_key, result)
    return result


def state_payload() -> dict:
    raw_rows = subscription_rows()
    rows = logical_subscription_rows(raw_rows)
    missing_queue = missing_queue_state()
    cron_log = read_text(CONFIG_DIR / ".cron.log", 60000)
    manual_log = read_text(MANUAL_LOG, 30000)
    return {
        "version": APP_VERSION,
        "generatedAt": iso_time(time.time()),
        "subscriptions": rows,
        "auth": auth_state(),
        "allPaused": all_paused(),
        "lastError": last_error_state(),
        "job": job_state(),
        "missingQueue": missing_queue,
        "scheduleActive": (CONFIG_DIR / "cron").exists() and not all_paused() and not missing_queue,
        "logs": {"scheduled": cron_log, "manual": manual_log},
        "latestDownloads": grouped_downloads(latest_downloads(raw_rows), raw_rows),
        "storage": {
            "videos": disk_state("/media/videos"),
            "music": disk_state("/media/music"),
        },
        "totals": {
            "subscriptions": len(rows),
            "video": sum(1 for row in rows if row["mode"] == "video"),
            "audio": sum(1 for row in rows if row["mode"] == "audio" or row.get("additionalAudio")),
            "archived": sum(row["archiveCount"] for row in rows),
            "paused": sum(1 for row in rows if row["paused"]),
        },
    }


def settings_payload() -> dict:
    config = load_config()
    presets = config.get("presets") or {}
    video = presets.get(selected_preset(presets, "video")) or {}
    audio = presets.get(selected_preset(presets, "audio")) or {}
    video_output = video.get("output_options") or {}
    audio_output = audio.get("output_options") or {}
    video_ytdl = video.get("ytdl_options") or {}
    audio_ytdl = audio.get("ytdl_options") or {}
    video_tags = video.get("video_tags") or {}
    music_tags = audio.get("music_tags") or {}
    logical_rows = {row["id"]: row for row in logical_subscription_rows()}
    reverse_links = {audio: primary for primary, audio in linked_audio_outputs().items()}
    schedules = []
    for schedule in schedule_definitions().values():
        members = list(dict.fromkeys(reverse_links.get(name, name) for name in schedule["subscriptions"]))
        labels = list(dict.fromkeys(
            logical_rows.get(name, {}).get("name") or DISPLAY_NAMES.get(name, name.replace("_", " ").title())
            for name in members
        ))
        if not labels:
            label = schedule["id"].replace("-", " ").title()
        elif len(labels) == 1:
            label = labels[0]
        elif len(labels) == 2:
            label = " & ".join(labels)
        else:
            label = f"{', '.join(labels[:-1])} & {labels[-1]}"
        schedules.append({**schedule, "label": label, "subscriptions": members})
    return {
        "workingDirectory": (config.get("configuration") or {}).get("working_directory", "/config/working"),
        **dashboard_settings(),
        "video": {
            "format": video.get("format", ""),
            "fileName": video_output.get("file_name", ""),
            "archiveName": video_output.get("download_archive_name", ""),
            "maintainArchive": bool(video_output.get("maintain_download_archive", True)),
            "syncWithSource": bool(video_output.get("sync_with_source", False)),
            "breakOnExisting": bool(video_ytdl.get("break_on_existing", False)),
            "mergeFormat": video_ytdl.get("merge_output_format", "mp4"),
            "titleTag": video_tags.get("title", "{title}"),
            "artistTag": video_tags.get("artist", "{uploader}"),
        },
        "audio": {
            "format": audio.get("format", ""),
            "fileName": audio_output.get("file_name", ""),
            "archiveName": audio_output.get("download_archive_name", ""),
            "maintainArchive": bool(audio_output.get("maintain_download_archive", True)),
            "syncWithSource": bool(audio_output.get("sync_with_source", False)),
            "breakOnExisting": bool(audio_ytdl.get("break_on_existing", False)),
            "codec": (audio.get("audio_extract") or {}).get("codec", "m4a"),
            "embedThumbnail": bool(audio.get("embed_thumbnail", True)),
            "titleTag": music_tags.get("title", "{title}"),
            "artistTag": music_tags.get("artist", "{uploader}"),
        },
        "schedules": schedules,
    }


def safe_backup(path: Path) -> None:
    backup_dir = CONFIG_DIR / ".dashboard-backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, backup_dir / f"{path.name}.{stamp}.bak")


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    tmp = path.with_name(f".{path.name}.dashboard-{os.getpid()}-{secrets.token_hex(3)}")
    tmp.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    else:
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def validate_file(name: str, content: str) -> None:
    if name.endswith(".yaml"):
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict):
            raise ValueError(f"{name} must contain a YAML mapping")
        if name == "subscriptions.yaml" and not ({*VIDEO_PRESETS, *AUDIO_PRESETS} & set(parsed)):
            raise ValueError("subscriptions.yaml must contain a video or audio preset group")
        return
    if name == "cron":
        check = subprocess.run(["/bin/bash", "-n"], input=content, text=True, capture_output=True, timeout=10)
        if check.returncode:
            raise ValueError(check.stderr.strip() or "cron script has invalid shell syntax")


def save_file(name: str, content: str) -> None:
    if name not in ALLOWED_FILES:
        raise ValueError("That file cannot be edited here")
    validate_file(name, content)
    path = CONFIG_DIR / name
    safe_backup(path)
    atomic_write(path, content, 0o755 if name == "cron" else None)


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def apply_subscription_fields(item: dict, mode: str, payload: dict) -> None:
    url = str(payload.get("url", "")).strip()
    output_dir = str(payload.get("outputDir", "")).strip()
    genre = str(payload.get("genre", "")).strip()
    format_value = str(payload.get("format", "")).strip()
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Metadata settings must be an object")
    if not url.startswith(("https://www.youtube.com/", "https://youtube.com/", "https://youtu.be/")):
        raise ValueError("Download URL must be a YouTube URL")
    if not output_dir.startswith(("/media/videos/", "/media/music/")):
        raise ValueError("Output folder must be inside /media/videos or /media/music")
    item["download"] = url
    item.setdefault("overrides", {})["output_dir"] = output_dir
    if format_value and format_value != "Preset default":
        item["format"] = format_value
    else:
        item.pop("format", None)
    tags_key = "video_tags" if mode == "video" else "music_tags"
    tags = item.get(tags_key) or {}
    for payload_key, tag_key in (("title", "title"), ("artist", "artist"), ("album", "album"), ("albumArtist", "albumartist"), ("date", "date")):
        value = str(metadata.get(payload_key, "")).strip()
        if value:
            tags[tag_key] = value
        else:
            tags.pop(tag_key, None)
    if mode == "video":
        if genre:
            tags["genre"] = genre
        else:
            tags.pop("genre", None)
    elif genre:
        tags["genres"] = [genre]
    else:
        tags.pop("genres", None)
    if tags:
        item[tags_key] = tags
    else:
        item.pop(tags_key, None)
    if mode == "audio" and "embedThumbnail" in payload:
        item["embed_thumbnail"] = bool(payload.get("embedThumbnail"))


def companion_audio_id(subscription_id: str) -> str:
    base = subscription_id[:-6] if subscription_id.endswith("_video") else subscription_id
    return f"{base[:44]}_audio"


def youtube_source_key(value: str) -> tuple[str, str]:
    parsed = urlparse(str(value).strip())
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [""])[0]
    if playlist_id:
        return "playlist", playlist_id
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path.rstrip("/")
    if host == "youtu.be" and path:
        return "video", path.lstrip("/")
    video_id = (query.get("v") or [""])[0]
    if video_id:
        return "video", video_id
    return host, path


def companion_audio_payload(payload: dict) -> dict:
    result = {
        "url": payload.get("url"),
        "outputDir": payload.get("audioOutputDir") or payload.get("outputDir"),
        "genre": payload.get("genre"),
        "format": "",
        "metadata": copy.deepcopy(payload.get("metadata") or {}),
    }
    return result


def apply_auth(item: dict, payload: dict) -> None:
    if bool(payload.get("useAuth")) or re.search(r"[?&]list=WL(?:&|$)", str(item.get("download", ""))):
        item.setdefault("ytdl_options", {})["cookiefile"] = "/config/youtube_cookies.txt"


def patch_subscription(subscription_id: str, payload: dict) -> None:
    rows = {row["id"]: row for row in subscription_rows()}
    if subscription_id not in rows:
        raise ValueError("Unknown subscription")
    data = load_subscriptions()
    mode = rows[subscription_id]["mode"]
    preset = selected_preset(data, mode)
    item = (data.get(preset) or {}).get(subscription_id)
    if not isinstance(item, dict):
        raise ValueError("Subscription block was not found")
    apply_subscription_fields(item, mode, payload)

    mapping = linked_audio_outputs()
    audio_id = mapping.get(subscription_id)
    wants_audio = mode == "video" and bool(payload.get("additionalAudio"))
    cron_path = CONFIG_DIR / "cron"
    cron = read_text(cron_path)
    group = rows[subscription_id]["scheduleGroup"]
    if wants_audio:
        if not str(payload.get("audioOutputDir") or payload.get("outputDir") or "").strip():
            raise ValueError("Choose a destination for the separate audio copy")
        audio_exists = bool(audio_id and audio_id in rows)
        linking_existing = False
        if not audio_id:
            audio_id = companion_audio_id(subscription_id)
            if audio_id in rows:
                existing_audio = rows[audio_id]
                if existing_audio["mode"] != "audio":
                    raise ValueError(f"Subscription ID {audio_id} already exists and is not an audio subscription")
                if youtube_source_key(existing_audio["url"]) != youtube_source_key(payload.get("url", "")):
                    raise ValueError(
                        f"Subscription ID {audio_id} already exists but uses a different YouTube source"
                    )
                if payload.get("confirmLinkExistingAudio") is not True:
                    primary_name = rows[subscription_id]["name"]
                    audio_name = existing_audio["name"]
                    raise ConfirmationRequired(
                        f"Confirm linking {subscription_id} with {audio_id}",
                        {
                            "type": "link-existing-audio",
                            "primaryId": subscription_id,
                            "primaryName": primary_name,
                            "audioId": audio_id,
                            "audioName": audio_name,
                            "message": (
                                f"Combine {primary_name} video and audio subscriptions?\n\n"
                                "They will appear as one Video + Audio subscription, and matching download history "
                                "will be grouped in Activity. Run, pause, and remove actions will apply to both.\n\n"
                                "Existing files, archives, destinations, and audio settings will be preserved."
                            ),
                        },
                    )
                audio_exists = True
                linking_existing = True
            mapping[subscription_id] = audio_id
        if linking_existing and rows[audio_id]["scheduleGroup"] != group:
            old_group = rows[audio_id]["scheduleGroup"]
            if old_group:
                cron = change_cron_member(cron, old_group, audio_id, False)
            cron = change_cron_member(cron, group, audio_id, True)
        if not audio_exists:
            cron = change_cron_member(cron, group, audio_id, True)
        audio_preset = selected_preset(data, "audio")
        audio_item = data.setdefault(audio_preset, {}).setdefault(audio_id, {})
        if not linking_existing:
            apply_subscription_fields(audio_item, "audio", companion_audio_payload(payload))
            apply_auth(audio_item, payload)
            if (item.get("ytdl_options") or {}).get("cookiefile"):
                audio_item.setdefault("ytdl_options", {})["cookiefile"] = item["ytdl_options"]["cookiefile"]
    elif audio_id:
        audio_row = rows.get(audio_id)
        if audio_row:
            audio_preset = selected_preset(data, "audio")
            data.get(audio_preset, {}).pop(audio_id, None)
            cron = change_cron_member(cron, audio_row["scheduleGroup"] or group, audio_id, False)
        mapping.pop(subscription_id, None)

    interval = int(payload.get("intervalMinutes", rows[subscription_id]["intervalMinutes"]))
    if interval < 1 or interval > 1440:
        raise ValueError("Interval must be between 1 and 1440 minutes")
    pattern = rf"(^run_if_due\s+{re.escape(group)}\s+)\d+(\s+)"
    changed, count = re.subn(pattern, rf"\g<1>{interval}\g<2>", cron, count=1, flags=re.MULTILINE)
    if not count:
        raise ValueError("Schedule group was not found in cron")
    updated = dump_yaml(data)
    validate_file("subscriptions.yaml", updated)
    validate_file("cron", changed)
    safe_backup(CONFIG_DIR / "subscriptions.yaml")
    atomic_write(CONFIG_DIR / "subscriptions.yaml", updated)
    safe_backup(cron_path)
    atomic_write(cron_path, changed, 0o755)
    update_linked_audio_outputs(mapping)
    paused_ids = paused_subscriptions()
    if audio_id and not wants_audio and audio_id in paused_ids:
        atomic_write(PAUSED_FILE, "".join(f"{name}\n" for name in sorted(paused_ids - {audio_id})), 0o600)


def change_cron_member(cron: str, group: str, subscription_id: str, add: bool) -> str:
    result = []
    found = False
    for line in cron.splitlines(keepends=True):
        parts = line.split("||", 1)
        tokens = parts[0].split()
        if len(tokens) < 3 or tokens[0] != "run_if_due" or tokens[1] != group:
            result.append(line)
            continue
        found = True
        members = tokens[3:]
        if add and subscription_id not in members:
            members.append(subscription_id)
        if not add:
            members = [name for name in members if name != subscription_id]
        suffix = " ||" + parts[1].rstrip("\n") if len(parts) == 2 else ""
        result.append(" ".join(tokens[:3] + members) + suffix + "\n")
    if not found:
        raise ValueError("Schedule group was not found in cron")
    return "".join(result)


def create_subscription(payload: dict) -> str:
    subscription_id = str(payload.get("id", "")).strip()
    mode = str(payload.get("mode", ""))
    group = str(payload.get("scheduleGroup", ""))
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,49}", subscription_id):
        raise ValueError("Use a subscription ID of 3–50 lowercase letters, numbers, or underscores")
    if mode not in {"video", "audio"}:
        raise ValueError("Choose audio or video")
    if group not in schedule_definitions():
        raise ValueError("Choose an existing schedule group")
    existing_ids = {row["id"] for row in subscription_rows()}
    if subscription_id in existing_ids:
        raise ValueError("Subscription ID already exists")
    data = load_subscriptions()
    preset = selected_preset(data, mode)
    item = {}
    apply_subscription_fields(item, mode, payload)
    apply_auth(item, payload)
    data.setdefault(preset, {})[subscription_id] = item
    mapping = linked_audio_outputs()
    audio_id = None
    if mode == "video" and bool(payload.get("additionalAudio")):
        if not str(payload.get("audioOutputDir") or payload.get("outputDir") or "").strip():
            raise ValueError("Choose a destination for the separate audio copy")
        audio_id = companion_audio_id(subscription_id)
        if audio_id in existing_ids or audio_id == subscription_id:
            raise ValueError(f"Subscription ID {audio_id} already exists")
        audio_item = {}
        apply_subscription_fields(audio_item, "audio", companion_audio_payload(payload))
        apply_auth(audio_item, payload)
        data.setdefault(selected_preset(data, "audio"), {})[audio_id] = audio_item
        mapping[subscription_id] = audio_id
    updated_yaml = dump_yaml(data)
    cron_path = CONFIG_DIR / "cron"
    updated_cron = change_cron_member(read_text(cron_path), group, subscription_id, True)
    if audio_id:
        updated_cron = change_cron_member(updated_cron, group, audio_id, True)
    validate_file("subscriptions.yaml", updated_yaml)
    validate_file("cron", updated_cron)
    yaml_path = CONFIG_DIR / "subscriptions.yaml"
    safe_backup(yaml_path)
    safe_backup(cron_path)
    atomic_write(yaml_path, updated_yaml)
    atomic_write(cron_path, updated_cron, 0o755)
    if audio_id:
        update_linked_audio_outputs(mapping)
    return subscription_id


def remove_subscription(subscription_id: str) -> None:
    rows = {row["id"]: row for row in subscription_rows()}
    row = rows.get(subscription_id)
    if not row:
        raise ValueError("Unknown subscription")
    data = load_subscriptions()
    mapping = linked_audio_outputs()
    linked_ids = linked_subscription_ids(subscription_id)
    primary_id = linked_ids[0]
    if subscription_id != primary_id:
        raise ValueError("Remove the combined subscription from its video entry")
    preset = selected_preset(data, row["mode"])
    del data[preset][subscription_id]
    audio_id = mapping.pop(subscription_id, None)
    if audio_id:
        data.get(selected_preset(data, "audio"), {}).pop(audio_id, None)
    updated_yaml = dump_yaml(data)
    cron_path = CONFIG_DIR / "cron"
    updated_cron = change_cron_member(read_text(cron_path), row["scheduleGroup"], subscription_id, False)
    if audio_id:
        audio_row = rows.get(audio_id)
        updated_cron = change_cron_member(updated_cron, (audio_row or row)["scheduleGroup"], audio_id, False)
    validate_file("subscriptions.yaml", updated_yaml)
    validate_file("cron", updated_cron)
    yaml_path = CONFIG_DIR / "subscriptions.yaml"
    safe_backup(yaml_path)
    safe_backup(cron_path)
    atomic_write(yaml_path, updated_yaml)
    atomic_write(cron_path, updated_cron, 0o755)
    update_linked_audio_outputs(mapping)
    paused_ids = paused_subscriptions()
    removed_ids = {subscription_id, *( [audio_id] if audio_id else [] )}
    if paused_ids & removed_ids:
        atomic_write(PAUSED_FILE, "".join(f"{name}\n" for name in sorted(paused_ids - removed_ids)), 0o600)


def save_schedule_settings(payload: dict) -> None:
    schedules = payload.get("schedules")
    if not isinstance(schedules, list):
        raise ValueError("schedules must be a list")
    definitions = schedule_definitions()
    requested = {}
    for item in schedules:
        if not isinstance(item, dict):
            continue
        group = str(item.get("id", ""))
        if group not in definitions:
            raise ValueError(f"Unknown schedule group: {group}")
        interval = int(item.get("intervalMinutes", 0))
        if interval < 1 or interval > 1440:
            raise ValueError("Schedule intervals must be between 1 and 1440 minutes")
        requested[group] = interval
    if set(requested) != set(definitions):
        raise ValueError("All schedule groups are required")
    timeout = int(payload.get("missingQueueTimeoutMinutes", DEFAULT_MISSING_QUEUE_TIMEOUT))
    if timeout < 5 or timeout > 120:
        raise ValueError("Missing queue timeout must be between 5 and 120 minutes")
    path = CONFIG_DIR / "cron"
    cron = read_text(path)
    updated = cron
    for group, interval in requested.items():
        pattern = rf"(^run_if_due\s+{re.escape(group)}\s+)\d+(\s+)"
        updated, count = re.subn(pattern, rf"\g<1>{interval}\g<2>", updated, count=1, flags=re.MULTILINE)
        if not count:
            raise ValueError(f"Schedule group {group} was not found in cron")
    validate_file("cron", updated)
    safe_backup(path)
    atomic_write(path, updated, 0o755)
    settings = load_dashboard_settings()
    settings["missingQueueTimeoutMinutes"] = timeout
    save_dashboard_settings(settings)


def save_config_settings(payload: dict) -> None:
    config = load_config()
    configuration = config.setdefault("configuration", {})
    working_directory = str(payload.get("workingDirectory", "")).strip()
    if not working_directory.startswith("/config/"):
        raise ValueError("Working directory must be inside /config")
    configuration["working_directory"] = working_directory
    presets = config.setdefault("presets", {})

    def apply_preset(mode: str, form: dict) -> None:
        if not isinstance(form, dict):
            raise ValueError(f"{mode} settings are required")
        preset_name = selected_preset(presets, mode)
        preset = presets.setdefault(preset_name, {})
        preset["format"] = str(form.get("format", "")).strip()
        output = preset.setdefault("output_options", {})
        output["output_directory"] = "{output_dir}"
        output["file_name"] = str(form.get("fileName", "")).strip()
        output["download_archive_name"] = str(form.get("archiveName", "")).strip()
        output["maintain_download_archive"] = bool(form.get("maintainArchive"))
        output["sync_with_source"] = bool(form.get("syncWithSource"))
        ytdl = preset.setdefault("ytdl_options", {})
        ytdl["break_on_existing"] = bool(form.get("breakOnExisting"))
        if mode == "video":
            ytdl["merge_output_format"] = str(form.get("mergeFormat", "mp4")).strip() or "mp4"
            tags = preset.setdefault("video_tags", {})
        else:
            preset.setdefault("audio_extract", {})["codec"] = str(form.get("codec", "m4a")).strip() or "m4a"
            preset["embed_thumbnail"] = bool(form.get("embedThumbnail"))
            tags = preset.setdefault("music_tags", {})
        tags["title"] = str(form.get("titleTag", "{title}")).strip()
        tags["artist"] = str(form.get("artistTag", "{uploader}")).strip()
        if not preset["format"] or not output["file_name"] or not output["download_archive_name"]:
            raise ValueError(f"{mode.title()} format and file templates cannot be empty")

    apply_preset("video", payload.get("video"))
    apply_preset("audio", payload.get("audio"))
    updated = dump_yaml(config)
    validate_file("config.yaml", updated)
    path = CONFIG_DIR / "config.yaml"
    safe_backup(path)
    atomic_write(path, updated)


def finish_job(process: subprocess.Popen, log_handle, job_id: str, on_finish=None) -> None:
    global CURRENT_JOB
    code = process.wait()
    finished = iso_time(time.time())
    finish_error = None
    if on_finish:
        try:
            on_finish(code)
        except Exception as exc:
            finish_error = exc
            code = code or 1
            log_handle.write(f"\n[{finished}] Finalization failed: {exc}\n")
    log_handle.write(f"\n[{finished}] Dashboard job finished with exit code {code}.\n")
    log_handle.close()
    if code:
        atomic_write(LAST_ERROR_FILE, f"{finished} · Dashboard job failed with exit code {code}.\n", 0o600)
    else:
        LAST_ERROR_FILE.unlink(missing_ok=True)
    try:
        schedule_lock_dir().rmdir()
    except OSError:
        pass
    with JOB_LOCK:
        if CURRENT_JOB and CURRENT_JOB.get("id") == job_id:
            CURRENT_JOB = None
    if finish_error:
        print(f"Dashboard job finalization failed: {finish_error}", flush=True)


def start_job(kind: str, label: str, command: list[str], *, reuse_lock: bool = False, on_finish=None) -> dict:
    global CURRENT_JOB
    with JOB_LOCK:
        if CURRENT_JOB or (schedule_lock_dir().exists() and not reuse_lock):
            raise RuntimeError("Another scheduled or manual check is already running")
        if reuse_lock:
            if not schedule_lock_dir().exists():
                raise RuntimeError("The missing-media review lock was lost")
        else:
            schedule_lock_dir().mkdir()
        job_id = secrets.token_hex(6)
        CURRENT_JOB = {
            "id": job_id,
            "kind": kind,
            "label": label,
            "startedAt": iso_time(time.time()),
            "running": True,
        }
    log_handle = MANUAL_LOG.open("a", encoding="utf-8")
    log_handle.write(f"\n[{CURRENT_JOB['startedAt']}] {label}\n$ {' '.join(command)}\n")
    log_handle.flush()
    try:
        process = subprocess.Popen(
            command,
            cwd=CONFIG_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except Exception:
        log_handle.close()
        try:
            schedule_lock_dir().rmdir()
        except OSError:
            pass
        with JOB_LOCK:
            CURRENT_JOB = None
        raise
    threading.Thread(target=finish_job, args=(process, log_handle, job_id, on_finish), daemon=True).start()
    return dict(CURRENT_JOB)


def start_missing_recovery(selected_keys: list[str]) -> dict:
    state = missing_queue_state()
    if not state or state.get("status") != "reviewing":
        raise ValueError("Start a missing-media review first")
    if not selected_keys:
        raise ValueError("Select at least one missing item")
    indexed = {item.get("key"): item for item in state["items"] if isinstance(item, dict)}
    if any(key not in indexed for key in selected_keys):
        raise ValueError("The missing-media selection is out of date")
    selected = [indexed[key] for key in dict.fromkeys(selected_keys)]

    subscriptions = load_subscriptions()
    rows = {row["id"]: row for row in subscription_rows()}
    grouped: dict[str, list[dict]] = {}
    for item in selected:
        grouped.setdefault(item["subscriptionId"], []).append(item)

    token = secrets.token_hex(5)
    temporary_subscriptions = {}
    contexts = []
    for subscription_id, items in grouped.items():
        row = rows.get(subscription_id)
        if not row:
            raise ValueError(f"Subscription {subscription_id} no longer exists")
        preset_name = selected_preset(subscriptions, row["mode"])
        source_group = subscriptions.get(preset_name)
        if not isinstance(source_group, dict) or subscription_id not in source_group:
            raise ValueError(f"Cannot locate subscription {subscription_id}")
        raw = source_group[subscription_id]
        recovered = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        urls = [item["sourceUrl"] for item in items]
        recovered["download"] = urls[0] if len(urls) == 1 else urls
        temporary_archive_name = f".yt-playlist-archiver-recovery-{token}-{subscription_id}.json"
        output_options = recovered.get("output_options")
        if not isinstance(output_options, dict):
            output_options = {}
        output_options.update({
            "download_archive_name": temporary_archive_name,
            "maintain_download_archive": True,
            "sync_with_source": False,
        })
        recovered["output_options"] = output_options
        temporary_subscriptions.setdefault(preset_name, {})[subscription_id] = recovered
        contexts.append({
            "subscriptionId": subscription_id,
            "videoIds": [item["videoId"] for item in items],
            "originalArchive": str(subscription_archive_path(row)),
            "temporaryArchive": str(Path(row["outputDir"]) / temporary_archive_name),
        })

    temporary_file = CONFIG_DIR / f".yt-playlist-archiver-recovery-{token}.yaml"
    atomic_write(temporary_file, dump_yaml(temporary_subscriptions), 0o600)
    state.update({
        "status": "downloading",
        "selectedKeys": [item["key"] for item in selected],
        "temporarySubscriptionFile": str(temporary_file),
        "recoveryContexts": contexts,
    })
    write_missing_queue(state)

    def finish_recovery(_code: int) -> None:
        merge_missing_recovery_archives(state)
        clear_missing_queue(state)

    command = [
        "ytdl-sub", "sub", str(temporary_file), "--suppress-colors", "--log-level", "verbose", "--match",
        *sorted(grouped),
    ]
    try:
        return start_job(
            "missing-recovery",
            f"Download {len(selected)} checked missing item{'s' if len(selected) != 1 else ''}",
            command,
            reuse_lock=True,
            on_finish=finish_recovery,
        )
    except Exception:
        state["status"] = "reviewing"
        state.pop("selectedKeys", None)
        cleanup_missing_queue_artifacts(state)
        state.pop("temporarySubscriptionFile", None)
        state.pop("recoveryContexts", None)
        write_missing_queue(state)
        raise


def make_session(secret: str) -> str:
    expiry = int(time.time()) + 12 * 60 * 60
    nonce = secrets.token_urlsafe(12)
    message = f"{expiry}.{nonce}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}.{signature}"


def valid_session(token: str, secret: str) -> bool:
    try:
        expiry_text, nonce, signature = token.split(".", 2)
        if int(expiry_text) < int(time.time()):
            return False
        expected = hmac.new(secret.encode(), f"{expiry_text}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "YTDLSubDashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload, status=HTTPStatus.OK, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_asset(self, name: str, content_type: str) -> None:
        path = APP_DIR / name
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: https://i.ytimg.com; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def parse_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Invalid request size")
        parsed = json.loads(self.rfile.read(length))
        if not isinstance(parsed, dict):
            raise ValueError("JSON object required")
        return parsed

    def is_authenticated(self) -> bool:
        password = configured_password()
        secret = configured_secret()
        if not password:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("ytdl_session")
        return bool(token and secret and valid_session(token.value, secret))

    def require_auth(self) -> bool:
        if self.is_authenticated():
            return True
        self.send_json({"error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_asset("index.html", "text/html; charset=utf-8")
        elif path == "/app.css":
            self.send_asset("app.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self.send_asset("app.js", "text/javascript; charset=utf-8")
        elif path == "/format.js":
            self.send_asset("format.js", "text/javascript; charset=utf-8")
        elif path == "/app-icon.png":
            self.send_asset("app-icon.png", "image/png")
        elif path == "/api/session":
            self.send_json({"authenticated": self.is_authenticated(), "passwordRequired": bool(configured_password())})
        elif path == "/api/state":
            if not self.require_auth():
                return
            try:
                self.send_json(state_payload())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/progress":
            if not self.require_auth():
                return
            try:
                self.send_json(progress_payload())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/settings":
            if not self.require_auth():
                return
            try:
                self.send_json(settings_payload())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path.startswith("/api/file/"):
            if not self.require_auth():
                return
            name = unquote(path.removeprefix("/api/file/"))
            if name not in ALLOWED_FILES:
                self.send_json({"error": "Unknown file"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"name": name, "content": read_text(CONFIG_DIR / name)})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.parse_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/login":
            password = configured_password()
            supplied = str(payload.get("password", ""))
            if password and not hmac.compare_digest(supplied, password):
                self.send_json({"error": "Incorrect password"}, HTTPStatus.UNAUTHORIZED)
                return
            secret = configured_secret()
            cookie = f"ytdl_session={make_session(secret)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"
            self.send_json({"ok": True}, headers={"Set-Cookie": cookie})
            return

        if not self.require_auth():
            return
        try:
            if path == "/api/missing-queue/start":
                queue = start_missing_queue()
                self.send_json({"ok": True, "missingQueue": queue})
            elif path == "/api/missing-queue/cancel":
                cancel_missing_queue()
                self.send_json({"ok": True})
            elif path == "/api/missing-queue/download":
                keys = payload.get("keys")
                if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                    raise ValueError("keys must be a list of missing item IDs")
                self.send_json({"job": start_missing_recovery(keys)}, HTTPStatus.ACCEPTED)
            elif path == "/api/run":
                if all_paused():
                    raise ValueError("Resume all subscriptions before running a check")
                requested = payload.get("subscriptions") or []
                if not isinstance(requested, list):
                    raise ValueError("subscriptions must be a list")
                raw_rows = subscription_rows()
                rows = logical_subscription_rows(raw_rows)
                known = {row["id"] for row in rows}
                paused = {row["id"] for row in rows if row["paused"]}
                names = [str(name) for name in requested]
                if not names:
                    names = sorted(known - paused)
                if any(name not in known for name in names):
                    raise ValueError("Unknown subscription selected")
                selected_paused = sorted(set(names) & paused)
                if selected_paused:
                    raise ValueError(f"Resume paused subscriptions before running them: {', '.join(selected_paused)}")
                if not names:
                    raise ValueError("All subscriptions are paused")
                raw_names = []
                for name in names:
                    raw_names.extend(linked_subscription_ids(name))
                names = list(dict.fromkeys(raw_names))
                dry_run = bool(payload.get("dryRun", False))
                command = ["ytdl-sub"]
                if dry_run:
                    command.append("--dry-run")
                command.extend(["sub", "--suppress-colors", "--log-level", "verbose", "--match", *names])
                label = ("Preview " if dry_run else "Run ") + ", ".join(names)
                self.send_json({"job": start_job("dry-run" if dry_run else "manual", label, command)}, HTTPStatus.ACCEPTED)
            elif path == "/api/auth/check":
                self.send_json({"job": start_job("authentication", "Check YouTube Watch Later authentication", ["/config/youtube-auth-check.sh"])}, HTTPStatus.ACCEPTED)
            elif path == "/api/auth/browser/start":
                self.send_json({"ok": True, "container": set_firefox_container(True)})
            elif path == "/api/auth/browser/stop":
                self.send_json({"ok": True, "container": set_firefox_container(False)})
            elif path == "/api/auth/import-firefox":
                container = firefox_container_state()
                if container["available"] and container["running"]:
                    set_firefox_container(False)
                content, count = firefox_cookie_text()
                self.send_json({"ok": True, "cookieCount": count, "browserStopped": container["available"], "auth": install_youtube_cookies(content)})
            elif path == "/api/auth/import-file":
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("Cookie file content is required")
                self.send_json({"ok": True, "auth": install_youtube_cookies(content)})
            elif path == "/api/subscription":
                subscription_id = str(payload.get("id", ""))
                patch_subscription(subscription_id, payload)
                self.send_json({"ok": True, "subscription": subscription_id})
            elif path == "/api/subscription/create":
                if CURRENT_JOB or schedule_lock_dir().exists():
                    raise RuntimeError("Wait for the current check to finish before changing subscriptions")
                subscription_id = create_subscription(payload)
                self.send_json({"ok": True, "subscription": subscription_id}, HTTPStatus.CREATED)
            elif path == "/api/subscription/remove":
                if CURRENT_JOB or schedule_lock_dir().exists():
                    raise RuntimeError("Wait for the current check to finish before changing subscriptions")
                subscription_id = str(payload.get("id", ""))
                remove_subscription(subscription_id)
                self.send_json({"ok": True, "subscription": subscription_id})
            elif path == "/api/subscription/pause":
                subscription_id = str(payload.get("id", ""))
                paused = bool(payload.get("paused"))
                set_subscription_paused(subscription_id, paused)
                self.send_json({"ok": True, "subscription": subscription_id, "paused": paused})
            elif path == "/api/subscriptions/pause-all":
                paused = payload.get("paused")
                if not isinstance(paused, bool):
                    raise ValueError("paused must be true or false")
                set_all_paused(paused)
                self.send_json({"ok": True, "allPaused": paused})
            elif path == "/api/settings/config":
                save_config_settings(payload)
                self.send_json({"ok": True})
            elif path == "/api/settings/schedule":
                save_schedule_settings(payload)
                self.send_json({"ok": True})
            elif path.startswith("/api/file/"):
                name = unquote(path.removeprefix("/api/file/"))
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("File content must be text")
                save_file(name, content)
                self.send_json({"ok": True, "name": name})
            else:
                self.send_json({"error": "Unknown action"}, HTTPStatus.NOT_FOUND)
        except ConfirmationRequired as exc:
            self.send_json(
                {"error": str(exc), "confirmationRequired": True, "confirmation": exc.confirmation},
                HTTPStatus.CONFLICT,
            )
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    initialized = ensure_default_files()
    if initialized:
        print(f"Created starter configuration: {', '.join(initialized)}", flush=True)
    if enable_download_progress_logs():
        print("Enabled transfer progress in the existing scheduler", flush=True)

    def watch_archives() -> None:
        while True:
            try:
                missing_queue_state()
                latest_downloads(subscription_rows())
            except Exception as exc:
                print(f"Download history update failed: {exc}", flush=True)
            time.sleep(15)

    threading.Thread(target=watch_archives, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"ytdl-sub dashboard listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()

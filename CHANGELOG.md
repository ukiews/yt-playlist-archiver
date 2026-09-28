# Version history

The version shown in the dashboard comes from `web/VERSION`. Each release updates that file, adds a section here, and receives a matching `vX.Y.Z` Git tag. Versioned container images use the same tag.

## 1.1.0 — 2026-09-28

- Find previously downloaded media that is missing from storage, review it with thumbnails and subscription details, and download only the checked items with their original subscription settings.
- Pause automatic checks during missing-media review and recovery, with a configurable 15-minute review timeout and safe recovery after cancellation, expiration, or restart.
- Save a separate audio copy from a video subscription using the Global Audio Preset, with one schedule, pause control, and subscription entry for both outputs.
- Group linked video and audio files into one Downloads Activity entry with the actual file formats shown as tags.
- Keep video and audio download archives independent so each output remains protected from duplicate downloads.

## 1.0.1 — 2026-09-27

- Make subscription format inheritance explicit with **Use Global Defaults**, while showing the inherited Configuration format settings as disabled fields.
- Restore both services automatically after host restarts and allow the dashboard to start before the host receives its network address.

## 1.0.0 — 2026-09-22

- Manage video and audio subscriptions from the browser, including adding, editing, removing, running, pausing, and resuming individual subscriptions or the full schedule.
- Run configurable schedule groups alongside manual checks, with persistent download archives that prevent previously downloaded media from being fetched again.
- Follow active downloads in Downloads Activity with thumbnails, playlist and channel details, transfer percentage, speed, ETA, processing status, filters, and persistent download history.
- Configure video formats through guided quality, resolution, container, codec, and advanced yt-dlp controls.
- Edit output folders, filenames, genres, artwork, and embedded title, artist, album, album artist, and date metadata.
- Preserve real YouTube album metadata and use a neutral `Unknown Album` fallback when no album is supplied.
- Authenticate private YouTube playlists such as Watch Later through an on-demand Firefox service that imports and verifies the session, then stops to release resources.
- Inspect technical activity logs and see scheduler, authentication, storage, error, last-download, and version status from the dashboard.
- Create working presets, subscription storage, schedule groups, and authentication checks automatically on first start.
- Use light and dark themes with a responsive desktop and mobile interface.
- Deploy on `linux/amd64` or `linux/arm64` with a minimal Compose configuration for public sources or the full Compose stack for private-playlist authentication and configurable storage.
- Keep timestamped backups when configuration is changed through the dashboard.

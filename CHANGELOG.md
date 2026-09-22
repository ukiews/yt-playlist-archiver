# Version history

The version shown in the dashboard comes from `web/VERSION`. Each release updates that file, adds a section here, and receives a matching `vX.Y.Z` Git tag. Versioned container images use the same tag.

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

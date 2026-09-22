FROM ghcr.io/jmbannon/ytdl-sub:latest

ARG VCS_REF="unknown"
ARG BUILD_DATE="unknown"
LABEL org.opencontainers.image.title="YT Playlist Archiver" \
      org.opencontainers.image.description="Monitor YouTube playlists and automatically archive new video or audio with ytdl-sub" \
      org.opencontainers.image.source="https://github.com/ukiews/yt-playlist-archiver" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.licenses="MIT"

USER root
COPY web/ /app/
RUN python -m py_compile /app/server.py \
    && chmod -R a=rX /app
USER 1000:10

ENV PYTHONDONTWRITEBYTECODE=1 \
    YTDL_SUB_CONFIG_DIR=/config \
    DASHBOARD_PORT=8787

EXPOSE 8787
ENTRYPOINT ["python", "/app/server.py"]

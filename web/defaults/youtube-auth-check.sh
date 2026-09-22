#!/bin/sh

set -u

cookie_file="/config/youtube_cookies.txt"
status_file="/config/youtube-auth-status.txt"
error_file="/config/youtube-auth-last-error.log"
marker_file="/config/AUTHENTICATION_REQUIRED"
probe_url="https://www.youtube.com/playlist?list=WL"
tmp_error="${error_file}.tmp"

umask 077

fail() {
    message="$1"
    printf '%s\n' "$message" > "$status_file"
    printf '%s\n' "$message" > "$marker_file"
    if [ -s "$tmp_error" ]; then
        mv -f "$tmp_error" "$error_file"
    else
        rm -f "$tmp_error"
    fi
    chmod 600 "$status_file" "$marker_file" 2>/dev/null || true
    [ ! -f "$error_file" ] || chmod 600 "$error_file"
    printf '%s\n' "$message" >&2
    exit 1
}

if [ ! -s "$cookie_file" ]; then
    fail "YouTube authentication is unavailable: $cookie_file is missing or empty."
fi

first_line=$(head -n 1 "$cookie_file" 2>/dev/null || true)
case "$first_line" in
    '# Netscape HTTP Cookie File'*|'# HTTP Cookie File'*) ;;
    *) fail "YouTube authentication is unavailable: the cookie file is not in Netscape format." ;;
esac

if python -m yt_dlp \
    --cookies "$cookie_file" \
    --flat-playlist \
    --playlist-end 1 \
    --dump-single-json \
    "$probe_url" >/dev/null 2>"$tmp_error"; then
    checked_at=$(date '+%Y-%m-%d %H:%M:%S %Z')
    printf 'YouTube Watch Later authentication succeeded at %s.\n' "$checked_at" > "$status_file"
    chmod 600 "$status_file"
    rm -f "$marker_file" "$error_file" "$tmp_error"
    cat "$status_file"
    exit 0
fi

fail "YouTube Watch Later authentication failed. Start the sign-in browser and refresh the saved session."

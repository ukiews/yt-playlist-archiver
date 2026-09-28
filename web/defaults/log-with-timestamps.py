#!/usr/bin/env python3
"""Prefix streamed command output with local ISO timestamps."""

import datetime as dt
import sys


def emit(buffer: bytearray) -> None:
    if not buffer:
        return
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    line = buffer.decode("utf-8", errors="replace")
    sys.stdout.write(f"[{stamp}] {line}\n")
    sys.stdout.flush()
    buffer.clear()


pending = bytearray()
while chunk := sys.stdin.buffer.read1(4096):
    for byte in chunk:
        if byte in (10, 13):
            emit(pending)
        else:
            pending.append(byte)
emit(pending)

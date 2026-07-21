"""
Streaming CSV autosave for the time-lapse heatmap.

Design (per discussion): opening/closing a file per frame is unnecessary I/O
cost and buys nothing extra in integrity. Instead the file is opened ONCE and
kept open; each logged frame is appended as one complete CSV row (so the file
is always parseable up to the last full row, even mid-run); rows are buffered
in memory and written as a chunk, then flush()'d — after flush() the data is in
the OS page cache, so a Python/GUI CRASH loses nothing beyond the current
partly-filled chunk. A periodic fsync() additionally covers an OS crash or power
loss (flush() alone doesn't guarantee the data reached the disk). The file is
closed cleanly on stop/clear/exit.

This intentionally runs independently of the rolling ring buffer used for the
on-screen heatmap, so a long run can be fully captured on disk even though only
the last N frames are kept for display.

Not SSD-wear motivated (timelapse rates are a handful of rows/s at most --
irrelevant next to wear-leveling and drive endurance); chunking is purely to
amortise syscall/GUI overhead.
"""
from __future__ import annotations

import os
import csv
import time
import threading


class HeatmapAutosaveWriter:
    def __init__(self, path: str, wavelengths, chunk_frames: int = 20,
                fsync_interval_s: float = 20.0):
        self.path = path
        self._chunk_frames = max(1, int(chunk_frames))
        self._fsync_interval_s = max(1.0, float(fsync_interval_s))
        self._lock = threading.Lock()
        self._buf: list[list] = []
        self._last_fsync = time.monotonic()
        self._closed = False
        # Open once; append mode with newline='' per csv module recommendation.
        self._f = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._f, delimiter=";")
        if self._f.tell() == 0:
            self._w.writerow(["Timestamp_ISO", "Elapsed_s"] +
                             [f"{wl:.2f}" for wl in wavelengths])
            self._f.flush()
        self._t0 = time.monotonic()

    def write_frame(self, timestamp_iso: str, spectrum) -> None:
        """Append one frame. Thread-safe (the acquisition/emit callback and the
        GUI thread can both reach this)."""
        with self._lock:
            if self._closed:
                return
            elapsed = time.monotonic() - self._t0
            self._buf.append([timestamp_iso, f"{elapsed:.3f}"] +
                             [round(float(v), 2) for v in spectrum])
            if len(self._buf) >= self._chunk_frames:
                self._flush_locked()
            elif (time.monotonic() - self._last_fsync) >= self._fsync_interval_s:
                # Time-based fsync even if the chunk isn't full yet, so a slow
                # (e.g. long-exposure) run still gets periodic crash safety.
                self._flush_locked()

    def _flush_locked(self) -> None:
        if self._buf:
            self._w.writerows(self._buf)
            self._buf.clear()
        self._f.flush()
        now = time.monotonic()
        if (now - self._last_fsync) >= self._fsync_interval_s:
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass    # e.g. unusual filesystem that doesn't support fsync
            self._last_fsync = now

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass
            self._f.close()
            self._closed = True

    @property
    def frames_written_pending(self) -> int:
        """Rows buffered but not yet flushed (for a status readout)."""
        with self._lock:
            return len(self._buf)

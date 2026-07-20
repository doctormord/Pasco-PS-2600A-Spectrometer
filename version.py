"""
Application version string — a datetime, read offline and copy-immune.

Source priority:
  1. A committed ``VERSION`` file  → the authoritative build datetime, written
     once when the version is made (by the GitHub Action or stamp_version.py).
     It travels WITH the code, so copying the folder never changes it, and no
     git or network is needed at runtime. This is the real version.
  2. git commit datetime  → used only if there's no VERSION file but a .git repo
     is present (still offline — reads the local repo, no network).
  3. newest source mtime  → last-resort guess; only meaningful on the machine
     the code was edited on. Prefixed with "~" so it's clearly approximate.
"""
from __future__ import annotations

import os
import subprocess
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHED: str | None = None


def _from_version_file() -> str | None:
    path = os.path.join(_HERE, "VERSION")
    if os.path.isfile(path):
        try:
            txt = open(path, encoding="utf-8").read().strip()
            return txt or None
        except OSError:
            return None
    return None


def _from_git() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
            cwd=_HERE, stderr=subprocess.DEVNULL, timeout=1.5,
        )
        return out.decode("utf-8", "replace").strip() or None
    except Exception:
        return None


def _from_mtime() -> str | None:
    newest = 0.0
    try:
        for name in os.listdir(_HERE):
            if name.endswith(".py"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(_HERE, name)))
                except OSError:
                    pass
    except OSError:
        pass
    if newest:
        return "~" + datetime.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
    return None


def app_version() -> str:
    global _CACHED
    if _CACHED is None:
        _CACHED = (_from_version_file() or _from_git() or _from_mtime()
                   or "unknown")
    return _CACHED


if __name__ == "__main__":
    print(app_version())

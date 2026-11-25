"""
data_paths.py — per-user data directory, path resolution and the one-time
migration of runtime data out of the program folder (Qt-free, no side
effects on import).

Runtime data (configuration, calibration profiles, dark spectra, linearity
records, lamp library, backups, calibration protocols) never lives in the
program / repository folder:

  Windows  %APPDATA%\\PS-2600A\\
  macOS    ~/Library/Application Support/PS-2600A/
  Linux    $XDG_CONFIG_HOME/ps-2600a/  (default ~/.config/ps-2600a/)

Override (highest first): the command-line option --data-dir PATH (handled by
the entry point via parse_cli), the environment variable PS2600A_DATA_DIR.

Migration (initialize → migrate): on the first start with a data directory
that has no migration marker, every runtime file found in the program folder
is COPIED (never moved) into the data directory; an existing file in the data
directory is never overwritten (the program-folder version is kept next to it
as <name>.from-program-dir-<timestamp>.json). Every source file is also copied
into backup/migration-<timestamp>/. The result is written to migration.json
(marker) and appended to migration.log. Afterwards the application reads and
writes only the data directory.

Exports default to <Documents>/PS-2600A (default_export_dir), never the
program folder.
"""
from __future__ import annotations

import filecmp
import glob
import json
import os
import shutil
import sys
from datetime import datetime

APP_NAME = "PS-2600A"
APP_NAME_XDG = "ps-2600a"
ENV_VAR = "PS2600A_DATA_DIR"
SKIP_MIGRATION_ENV = "PS2600A_SKIP_MIGRATION"
CLI_OPTION = "--data-dir"

CONFIG_FILE = "spectrometer_config.json"
# Runtime files that live in the data directory (and are migrated).
RUNTIME_FILES = (
    CONFIG_FILE,
    "calibration_profiles.json",
    "calibration_profiles.v1.bak.json",
    "dark_spectra.json",
    "reference_sources.json",
    "linearity_profiles.json",
)
# Further runtime leftovers in the program folder (backups, old exports of the
# calibration data, protocols written by older versions).
RUNTIME_GLOBS = ("*.bak.json", "old_calib_data_*.json")
PROTOCOL_GLOBS = ("calib_*.pdf", "calib_*.csv", "linearity_*.pdf", "linearity_*.csv")
# Config keys holding file names of runtime data (relative = data directory).
FILE_KEYS = ("calibration_profiles_file", "reference_sources_file",
             "dark_spectra_file", "linearity_profiles_file")
# Config keys holding directories that must not point into the program folder.
DIR_KEYS = ("export_dir", "heatmap_autosave_dir")

# Files that were tracked in the repository before this version: a `git pull`
# deletes them from the working copy BEFORE the first start. If they are
# missing in the program folder but the folder is a git working copy, the
# last committed version is restored from the git history into the data
# directory (never overwriting existing data).
GIT_RESTORE_FILES = ("calibration_profiles.json", "spectrometer_config.json")

MARKER = "migration.json"
LOG = "migration.log"
PROTOCOLS = "protocols"
BACKUPS = "backup"

_override: str | None = None


# ══════════════════════════════════════════════════════════════════════════
#  Locations
# ══════════════════════════════════════════════════════════════════════════

def program_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_data_dir(platform: str | None = None, env: dict | None = None,
                     home: str | None = None) -> str:
    """Platform default (without overrides)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if platform.startswith("win"):
        base = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, APP_NAME)
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", APP_NAME)
    base = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, APP_NAME_XDG)


def set_data_dir(path: str | None) -> None:
    """Command-line override (also exported as PS2600A_DATA_DIR so child
    processes, e.g. a headless server, use the same directory)."""
    global _override
    _override = os.path.abspath(os.path.expanduser(path)) if path else None
    if _override:
        os.environ[ENV_VAR] = _override


def data_dir() -> str:
    if _override:
        return _override
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return default_data_dir()


def ensure_data_dir() -> str:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def resolve(name_or_path: str) -> str:
    """Absolute paths stay; relative names resolve into the data directory."""
    p = os.path.expanduser(str(name_or_path or ""))
    if os.path.isabs(p):
        return p
    return os.path.join(data_dir(), p)


def config_path() -> str:
    return resolve(CONFIG_FILE)


def protocols_dir() -> str:
    return os.path.join(data_dir(), PROTOCOLS)


def backups_dir() -> str:
    return os.path.join(data_dir(), BACKUPS)


def documents_dir() -> str:
    """The user's documents folder (Windows known folder incl. redirection,
    XDG user dirs on Linux), falling back to the home directory."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 (My Documents), SHGFP_TYPE_CURRENT = 0
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:                                    # noqa: BLE001
            pass
        return os.path.join(home, "Documents")
    if sys.platform != "darwin":
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        try:
            with open(os.path.join(cfg, "user-dirs.dirs"), encoding="utf-8") as f:
                for line in f:
                    if line.startswith("XDG_DOCUMENTS_DIR="):
                        v = line.split("=", 1)[1].strip().strip('"')
                        v = v.replace("$HOME", home)
                        if v:
                            return v
        except OSError:
            pass
    d = os.path.join(home, "Documents")
    return d if os.path.isdir(d) else home


def default_export_dir() -> str:
    return os.path.join(documents_dir(), APP_NAME)


def is_inside(path: str, root: str) -> bool:
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:                       # different drives on Windows
        return False


# ══════════════════════════════════════════════════════════════════════════
#  Command line
# ══════════════════════════════════════════════════════════════════════════

def parse_cli(argv: list[str]) -> list[str]:
    """Remove '--data-dir PATH' / '--data-dir=PATH' from argv, apply it, and
    return the remaining arguments."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == CLI_OPTION and i + 1 < len(argv):
            set_data_dir(argv[i + 1])
            i += 2
            continue
        if a.startswith(CLI_OPTION + "="):
            set_data_dir(a.split("=", 1)[1])
            i += 1
            continue
        out.append(a)
        i += 1
    return out


# ══════════════════════════════════════════════════════════════════════════
#  Migration
# ══════════════════════════════════════════════════════════════════════════

def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sources(src_dir: str) -> list[str]:
    names = [n for n in RUNTIME_FILES if os.path.isfile(os.path.join(src_dir, n))]
    for pat in RUNTIME_GLOBS:
        for p in glob.glob(os.path.join(src_dir, pat)):
            n = os.path.basename(p)
            if os.path.isfile(p) and n not in names and not n.endswith(".example.json"):
                names.append(n)
    return names


def _git_restore(src: str, name: str) -> tuple[bytes | None, str]:
    """Content of `name` at the commit before it was deleted from the
    repository at `src` (None if not a git working copy / never tracked)."""
    if not os.path.isdir(os.path.join(src, ".git")):
        return None, ""
    import subprocess
    try:
        sha = subprocess.run(["git", "-C", src, "log", "-n", "1", "--format=%H",
                              "--diff-filter=D", "--", name], capture_output=True,
                             timeout=20, check=True).stdout.decode().strip()
        if not sha:
            return None, ""
        data = subprocess.run(["git", "-C", src, "show", f"{sha}^:{name}"],
                              capture_output=True, timeout=20, check=True).stdout
        return (data or None), f"{sha[:10]}^"
    except (OSError, subprocess.SubprocessError):
        return None, ""


def _fix_config(dst_cfg: str, src_dir: str, report: dict, stamp: str) -> None:
    """Point file/dir keys of the migrated config away from the program folder."""
    try:
        with open(dst_cfg, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        return
    changed = False
    for k in FILE_KEYS:
        v = cfg.get(k)
        if isinstance(v, str) and os.path.isabs(v) and is_inside(v, src_dir):
            base = os.path.basename(v)
            target = os.path.join(os.path.dirname(dst_cfg), base)
            if os.path.isfile(v) and not os.path.exists(target):
                shutil.copy2(v, target)
                report["files"].append({"name": base, "action": "copied", "from_key": k})
            cfg[k] = base
            changed = True
            report["config_changes"].append(f"{k}: {v} -> {base}")
    for k in DIR_KEYS:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip() and is_inside(v, src_dir):
            cfg[k] = ""
            changed = True
            report["config_changes"].append(f"{k}: {v} -> '' (default, outside the program folder)")
    if changed:
        with open(dst_cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)


def migrate(src_dir: str | None = None, dst_dir: str | None = None, *,
            log=print) -> dict:
    """One-time copy of runtime data from the program folder into the data
    directory (see module docstring). Idempotent: a second call returns
    {"status": "already_migrated"}. Never deletes or overwrites data."""
    src = os.path.abspath(src_dir or program_dir())
    dst = os.path.abspath(dst_dir or data_dir())
    os.makedirs(dst, exist_ok=True)
    marker = os.path.join(dst, MARKER)
    if os.path.exists(marker):
        return {"status": "already_migrated", "marker": marker}
    report = {"status": "migrated", "time": _now_iso(), "source": src, "target": dst,
              "files": [], "protocols": [], "config_changes": [], "backup": None,
              "errors": []}
    if os.path.normcase(src) == os.path.normcase(dst):
        report["status"] = "same_directory"
    else:
        stamp = _stamp()
        names = _sources(src)
        backup = os.path.join(dst, BACKUPS, f"migration-{stamp}")
        if names:
            os.makedirs(backup, exist_ok=True)
            report["backup"] = backup
        for n in names:
            sp = os.path.join(src, n)
            tp = os.path.join(dst, n)
            try:
                shutil.copy2(sp, os.path.join(backup, n))
                if not os.path.exists(tp):
                    shutil.copy2(sp, tp)
                    action = "copied"
                elif filecmp.cmp(sp, tp, shallow=False):
                    action = "identical"
                else:
                    root, ext = os.path.splitext(n)
                    keep = f"{root}.from-program-dir-{stamp}{ext}"
                    shutil.copy2(sp, os.path.join(dst, keep))
                    action = f"kept_existing (program-folder copy: {keep})"
                report["files"].append({"name": n, "action": action})
            except OSError as e:
                report["errors"].append(f"{n}: {e}")
        for n in GIT_RESTORE_FILES:
            tp = os.path.join(dst, n)
            if n in names or os.path.exists(tp):
                continue
            data, ref = _git_restore(src, n)
            if data:
                try:
                    with open(tp, "wb") as f:
                        f.write(data)
                    report["files"].append({"name": n, "action":
                                            f"restored_from_git ({ref}; file was removed "
                                            f"from the program folder by the update)"})
                except OSError as e:
                    report["errors"].append(f"{n}: {e}")
        prot = []
        for pat in PROTOCOL_GLOBS:
            prot += glob.glob(os.path.join(src, pat))
        if prot:
            pd = os.path.join(dst, PROTOCOLS)
            os.makedirs(pd, exist_ok=True)
            for p in sorted(set(prot)):
                n = os.path.basename(p)
                try:
                    if not os.path.exists(os.path.join(pd, n)):
                        shutil.copy2(p, os.path.join(pd, n))
                        report["protocols"].append(n)
                except OSError as e:
                    report["errors"].append(f"{n}: {e}")
        cfg = os.path.join(dst, CONFIG_FILE)
        if os.path.isfile(cfg):
            try:
                _fix_config(cfg, src, report, stamp)
            except OSError as e:
                report["errors"].append(f"config: {e}")
        if not report["files"] and not prot:
            report["status"] = "nothing_to_migrate"
    if report["errors"]:
        # no marker: the migration is retried on the next start (e.g. a file
        # locked by a virus scanner or a sync client); nothing was overwritten
        report["status"] = "partial"
    else:
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    try:
        with open(os.path.join(dst, LOG), "a", encoding="utf-8") as f:
            f.write(f"[{report['time']}] {report['status']}: {src} -> {dst}\n")
            for it in report["files"]:
                f.write(f"    {it['name']}: {it['action']}\n")
            for n in report["protocols"]:
                f.write(f"    protocols/{n}: copied\n")
            for c in report["config_changes"]:
                f.write(f"    config {c}\n")
            if report["backup"]:
                f.write(f"    backup: {report['backup']}\n")
            for e in report["errors"]:
                f.write(f"    ERROR {e}\n")
    except OSError:
        pass
    if log and report["status"] == "migrated":
        log(f"[data] migrated {len(report['files'])} file(s) from {src} to {dst} "
            f"(originals kept; backup {report['backup']})")
    return report


def seed_from_examples(dst_dir: str | None = None, src_dir: str | None = None,
                       skip=()) -> list[str]:
    """Create missing runtime files from the *.example.json templates of the
    program folder (fresh installation). Returns the created names."""
    src = os.path.abspath(src_dir or program_dir())
    dst = os.path.abspath(dst_dir or data_dir())
    os.makedirs(dst, exist_ok=True)
    made = []
    for n in RUNTIME_FILES:
        if n in skip:                  # a failed migration must not be masked
            continue
        root, ext = os.path.splitext(n)
        ex = os.path.join(src, f"{root}.example{ext}")
        tp = os.path.join(dst, n)
        if os.path.isfile(ex) and not os.path.exists(tp):
            shutil.copy2(ex, tp)
            made.append(n)
    return made


_initialized: dict | None = None


def initialize() -> dict:
    """Called once by app_config before the configuration is loaded: create
    the data directory, run the one-time migration (unless
    PS2600A_SKIP_MIGRATION is set), then seed missing files from the
    templates. Safe to call repeatedly."""
    global _initialized
    if _initialized is not None and _initialized.get("data_dir") == data_dir():
        return _initialized
    d = ensure_data_dir()
    rep: dict = {"data_dir": d}
    if not os.environ.get(SKIP_MIGRATION_ENV):
        try:
            rep["migration"] = migrate(program_dir(), d)
        except Exception as e:                                # noqa: BLE001
            rep["migration"] = {"status": "error", "error": str(e)}
            print(f"[data] migration failed: {e}")
    failed = {str(e).split(":", 1)[0] for e in
              (rep.get("migration") or {}).get("errors", [])}
    try:
        rep["seeded"] = seed_from_examples(d, skip=failed)
    except OSError as e:
        rep["seeded"] = []
        print(f"[data] seeding failed: {e}")
    _initialized = rep
    return rep

"""Running 24/7 on a home PC (mostly Windows): log file, one instance at a time,
no sleep while monitoring, console encoding, OneDrive pitfalls."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Iterator

log = logging.getLogger(__name__)

LOG_FILE = "ebeyparser.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
LOCK_FILE = "ebeyparser.lock"
HTTP_STATE_FILE = "http_state.json"  # hourly page window + block cooldowns, shared by monitor/CLI/web
_LOCK_OFFSET = 1 << 20  # Windows locks a byte far behind the PID text, so others can still read it
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def http_state_path(data_dir: str | Path) -> Path:
    """Where PoliteClient keeps its hourly page window and block cooldowns (one file for all
    commands, so a diagnostic run can't sneak past a cooldown the monitor is waiting out)."""
    return Path(data_dir) / HTTP_STATE_FILE


# ------------------------------------------------------------------ console
def force_utf8_console() -> None:
    """Emoji / Cyrillic must never crash printing (cp1252 consoles, redirected output).
    Under pythonw there is no console at all: give libraries a harmless sink."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - lives for the process
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# ---------------------------------------------------------------- log file
@contextmanager
def file_logging(log_dir: Path) -> Iterator[Path | None]:
    """Also log to <log_dir>/ebeyparser.log (5 MB x 5 files) while the block runs."""
    path = Path(log_dir) / LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: RotatingFileHandler | None = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=True)
    except OSError as exc:
        log.warning("Log file %s unavailable: %s", path, exc)
        handler = None
    root = logging.getLogger()
    if handler is not None:
        from .timefmt import LocalFormatter

        handler.setFormatter(LocalFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    try:
        yield path if handler is not None else None
    finally:
        if handler is not None:
            root.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------- single instance
class AlreadyRunningError(RuntimeError):
    def __init__(self, path: Path, info: str) -> None:
        pid, _, since = info.partition("\n")
        where = f" (PID {pid.strip()}" + (f", запущен {since.strip()}" if since.strip() else "") + ")" if pid.strip() else ""
        super().__init__(
            f"EbeyParser уже работает{where}. Два экземпляра сразу удвоят запросы к Kleinanzeigen и могут "
            "испортить базу. Закрой другое окно (или задачу в Планировщике) и запусти снова. "
            f"Если точно ничего не запущено — удали файл {path}."
        )
        self.path = path


def _try_lock(fh: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class InstanceLock:
    """data/ebeyparser.lock held with an OS file lock for the process lifetime.

    The OS drops the lock when the process dies, so a lock file left behind by a crash
    or power cut is never "stale": the next start simply takes it over."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - kept open while locked; never truncate before locking
        if not _try_lock(fh):
            fh.seek(0)
            info = fh.read(200).decode("utf-8", "replace")
            fh.close()
            raise AlreadyRunningError(self.path, info)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n{datetime.now():%d.%m.%Y %H:%M}\n".encode())
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        _unlock(self._fh)
        self._fh.close()
        self._fh = None  # the file stays: deleting it could race with a starting instance

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --------------------------------------------------------------- keep awake
@contextmanager
def keep_awake() -> Iterator[bool]:
    """Windows: don't let the PC fall asleep while monitoring (the screen may still turn off).
    No-op elsewhere. Yields whether it worked."""
    kernel32 = None
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if not kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                kernel32 = None
        except Exception:
            kernel32 = None
    try:
        yield kernel32 is not None
    finally:
        if kernel32 is not None:
            try:
                kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass


# ------------------------------------------------------------ console mode
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10


def disable_quick_edit() -> bool:
    """Windows console: turn off QuickEdit, so a stray mouse click can't freeze the program
    (in QuickEdit a click starts "select" mode and blocks all output until Enter/Esc).
    No-op (False) elsewhere or when stdin is not a console (pythonw, Task Scheduler, pipes)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        return new_mode == mode.value or bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False


# ------------------------------------------------------------- dependencies
PILLOW_WARNING = ("Pillow не установлен — фото уходят в нейросеть в полном размере, это медленно:"
                  " pip install -e .")
# the same for the web UI (no commands there)
PILLOW_WARNING_RU = ("Не хватает модуля для уменьшения фото — нейросеть будет работать медленнее. "
                     "Переустанови программу, чтобы это исправить")


def pillow_missing() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("PIL") is None
    except (ImportError, ValueError):
        return True


# -------------------------------------------------------------- one-time hints
def remembered(data_dir: str | Path, key: str) -> bool:
    """Was `key` already marked by once() for this data folder? (Does not mark it.)"""
    try:
        path = Path(data_dir) / ".hints"
        return path.is_file() and key in path.read_text(encoding="utf-8").split()
    except OSError:
        return False


def once(data_dir: str | Path, key: str) -> bool:
    """True the first time `key` is asked for this data folder (remembered in data/.hints)."""
    path = Path(data_dir) / ".hints"
    try:
        seen = set(path.read_text(encoding="utf-8").split()) if path.is_file() else set()
    except OSError:
        seen = set()
    if key in seen:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(key + "\n")
    except OSError:
        pass  # can't remember: showing a hint again is harmless
    return True


# ------------------------------------------------------------------ OneDrive
def onedrive_warning(data_dir: Path) -> str | None:
    """SQLite inside a synced OneDrive folder gets locked / corrupted: say so in plain words."""
    try:
        resolved = Path(data_dir).resolve()
    except OSError:
        return None
    roots = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    inside = any(part.lower().startswith("onedrive") for part in resolved.parts)
    for root in filter(None, roots):
        try:
            inside = inside or resolved.is_relative_to(Path(root).resolve())
        except (OSError, ValueError):
            continue
    if not inside:
        return None
    return (f"⚠ Папка с данными {resolved} лежит в OneDrive. Синхронизация может заблокировать или испортить"
            " базу. Перенеси папку программы, например в C:\\EbeyParser, или укажи general.data_dir вне OneDrive.")


__all__ = [
    "PILLOW_WARNING",
    "PILLOW_WARNING_RU",
    "AlreadyRunningError",
    "InstanceLock",
    "disable_quick_edit",
    "once",
    "pillow_missing",
    "remembered",
    "file_logging",
    "force_utf8_console",
    "http_state_path",
    "keep_awake",
    "onedrive_warning",
]

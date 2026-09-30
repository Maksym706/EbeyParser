"""This machine's hardware, cheaply: CPU cores, RAM, architecture and (when nvidia-smi is there)
the GPU — for «Выбери своё железо → рекомендую модель» (GET /api/v1/ai/recommend) and the scout's
expected speed before it has measured its own. Never raises; unknown values are None."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

ARM_MACHINES = frozenset({"arm64", "aarch64", "armv7l", "armv8l", "arm"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "ollama"})  # "ollama": the Docker service next to the app


def _ram_bytes() -> int | None:
    try:
        if sys.platform.startswith("win"):
            class _MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys)
            return None
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(size) if pages > 0 and size > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def _nvidia_gpu(timeout: float = 3.0) -> dict[str, Any] | None:
    """The biggest NVIDIA GPU via nvidia-smi (no driver / no tool = None)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    best: dict[str, Any] | None = None
    for line in out.splitlines():
        name, _, mem = line.rpartition(",")
        try:
            vram = round(float(mem.strip()) / 1024, 1)
        except ValueError:
            continue
        if best is None or vram > best["vram_gb"]:
            best = {"name": name.strip(), "vram_gb": vram}
    return best


@lru_cache(maxsize=2)
def detect_host(gpu: bool = True) -> dict[str, Any]:
    """{"cores", "ram_gb", "arch", "arm", "os", "gpu": {"name", "vram_gb"} | None}. Cached: the
    hardware doesn't change while the app runs. gpu=False skips nvidia-smi (fast path)."""
    ram = _ram_bytes()
    machine = (platform.machine() or "").lower()
    return {
        "cores": os.cpu_count(),
        "ram_gb": round(ram / 1024 ** 3, 1) if ram else None,
        "arch": machine or None,
        "arm": machine in ARM_MACHINES,
        "os": platform.system() or None,
        "gpu": _nvidia_gpu() if gpu else None,
    }


def is_local_url(url: str) -> bool:
    """Does an AI endpoint run on this machine (so this machine's hardware decides its speed)?"""
    if not (url or "").strip():
        return True  # the default local servers (LM Studio / Ollama on localhost)
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


__all__ = ["detect_host", "is_local_url"]

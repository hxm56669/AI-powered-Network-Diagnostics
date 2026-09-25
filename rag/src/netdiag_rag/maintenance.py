"""Local disk admission checks; model downloads in HF cache are outside the data quota."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class DiskCapacityError(RuntimeError):
    pass


def directory_bytes(root):
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                pass
    return total


def disk_status(settings, reserve_bytes=0):
    root = Path(settings["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    config = settings.get("maintenance", {})
    usage = directory_bytes(root)
    free = shutil.disk_usage(root).free
    maximum = config.get("max_data_bytes", 10 * 1024**3)
    minimum = config.get("min_free_bytes", 512 * 1024**2)
    fraction = config.get("warning_fraction", .85)
    blocked = usage + reserve_bytes >= maximum or free - reserve_bytes < minimum
    warning = blocked or usage + reserve_bytes >= maximum * fraction or free - reserve_bytes < minimum * 2
    return {"status": "blocked" if blocked else "warning" if warning else "ok",
            "data_bytes": usage, "free_bytes": free, "max_data_bytes": maximum,
            "min_free_bytes": minimum, "reserve_bytes": reserve_bytes}


def require_capacity(settings, reserve_bytes=0, *, nonessential=False):
    status = disk_status(settings, reserve_bytes)
    if status["status"] == "blocked" or (nonessential and status["status"] == "warning"):
        raise DiskCapacityError("disk_capacity_blocks_build" if nonessential else "disk_capacity_blocks_write")
    return status

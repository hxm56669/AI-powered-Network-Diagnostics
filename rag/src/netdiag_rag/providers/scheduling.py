"""Linux cross-process GPU admission, yielding offline work between batches.

The demand lock is shared by queued online jobs. Offline work may enter only
when it can take that lock exclusively and the device slot immediately. Both
locks are released by the kernel on process exit; no persistent waiter records.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time


def _try_lock(fd, mode):
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@contextmanager
def gpu_slot(device, priority="online", check=None):
    if not str(device).startswith("cuda"):
        if check:
            check()
        yield
        return
    if priority not in {"online", "offline"}:
        raise ValueError("invalid_model_priority")
    directory = Path(os.environ.get("NETDIAG_GPU_LOCK_DIR", f"/tmp/netdiag-gpu-{os.getuid()}"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("gpu_lock_directory_requires_private_ownership")
    # One conservative host-wide slot also handles cuda/cuda:0/visible-device remapping.
    files = []
    try:
        for name in ("online.lock", "device.lock"):
            fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            files.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("invalid_gpu_lock_file")
        demand, device_fd = files
        while True:
            if check:
                check()
            if priority == "online":
                if _try_lock(demand, fcntl.LOCK_SH):
                    while not _try_lock(device_fd, fcntl.LOCK_EX):
                        if check:
                            check()
                        time.sleep(.01)
                    break
            elif _try_lock(demand, fcntl.LOCK_EX):
                acquired = _try_lock(device_fd, fcntl.LOCK_EX)
                # Admit online waiters even during this offline batch.
                fcntl.flock(demand, fcntl.LOCK_UN)
                if acquired:
                    break
            time.sleep(.01)
        if check:
            check()
        yield
    finally:
        for fd in reversed(files):
            os.close(fd)

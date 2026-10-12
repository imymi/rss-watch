from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from functools import wraps

from .config import Config


def active(config: Config) -> bool:
    return (config.data_dir / "maintenance").exists()


@contextmanager
def data_lock(config: Config, *, exclusive: bool = False, timeout: float = 45):
    fd = os.open(config.data_dir / "data.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("正在等待维护操作完成，请稍后重试") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def business_write(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        with data_lock(self.config):
            if active(self.config):
                raise ValueError("系统处于维护模式，请先完成恢复并退出维护模式")
            return function(self, *args, **kwargs)
    return guarded

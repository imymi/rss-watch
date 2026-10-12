from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.request import urlopen

from .config import load_config
from .maintenance import active
from .service import Service


def configure_logging(config, role):
    directory = config.data_dir / "logs"
    directory.mkdir(mode=0o700, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file = RotatingFileHandler(directory / f"{role}.log", maxBytes=1024 * 1024,
                               backupCount=3, encoding="utf-8")
    file.setFormatter(formatter)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger = logging.getLogger("rss_watch")
    logger.setLevel(logging.INFO)
    logger.handlers = [file, stream]
    logger.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="RSS Watch 容器入口")
    parser.add_argument("role", choices=["web", "worker", "check-web", "check-worker"])
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("RSS_WATCH_CONFIG", "/app/container.toml")))
    args = parser.parse_args()
    config = load_config(args.config)
    if args.role == "check-web":
        try:
            with urlopen("http://127.0.0.1:8080/healthz", timeout=3) as response:
                sys.exit(0 if response.status == 200 else 1)
        except OSError:
            sys.exit(1)
    if args.role == "check-worker":
        sys.exit(0 if active(config) or Service(config).status()["后台运行"] else 1)
    configure_logging(config, args.role)
    if args.role == "web":
        import uvicorn

        from .web import create_app
        uvicorn.run(create_app(config), host="0.0.0.0", port=8080, workers=1,
                    access_log=False, forwarded_allow_ips=os.environ.get(
                        "RSS_WATCH_TRUSTED_PROXIES", "127.0.0.1"
                    ))
    else:
        from .worker import run_daemon
        asyncio.run(run_daemon(config))


if __name__ == "__main__":
    main()

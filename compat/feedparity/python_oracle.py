"""Read the fixture bytes and invoke the unmodified Python reference parser."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import feedparser

from rss_watch.domain import parse_feed


def main():
    if feedparser.__version__ != "6.0.14":
        raise SystemExit(f"Expected feedparser 6.0.14, got {feedparser.__version__}")
    base_url, *paths = sys.argv[1:]
    results = {}
    for path in map(Path, paths):
        try:
            articles = parse_feed(path.read_bytes(), base_url)
        except ValueError:
            articles = None
        results[path.name] = articles
    json.dump(results, sys.stdout)


if __name__ == "__main__":
    main()

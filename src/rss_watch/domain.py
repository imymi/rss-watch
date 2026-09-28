from __future__ import annotations

import hashlib
import html
import io
import json
import unicodedata
from html.parser import HTMLParser

import feedparser


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(text: str) -> str:
    parser = PlainText()
    parser.feed(str(text))
    return " ".join(unicodedata.normalize("NFKC", html.unescape("".join(parser.parts))).split())


def normalize_word(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


def match(rule: dict, title: str, summary: str) -> list[str]:
    # Keep the summary argument for callers; only the title affects matching.
    title = clean(title).casefold()
    includes = json.loads(rule["includes"])
    excludes = json.loads(rule["excludes"])
    if any(normalize_word(word) in title for word in excludes):
        return []
    hits = [word for word in includes if normalize_word(word) in title]
    return hits if hits and (rule["mode"] == "any" or len(hits) == len(includes)) else []


def parse_feed(content: bytes, base_url: str = "") -> list[dict]:
    # A stream prevents feedparser from treating a response body as a local filename.
    parsed = feedparser.parse(io.BytesIO(content), response_headers={
        "content-location": base_url, "content-type": "application/xml",
    })
    # Partial malformed XML must not silently establish an incomplete baseline.
    if not parsed.version or parsed.bozo:
        raise ValueError("订阅内容不是有效的 RSS/Atom，或 XML 格式损坏")
    articles = []
    for entry in parsed.entries:
        title = clean(entry.get("title", ""))
        published = entry.get("published", entry.get("updated", ""))
        link = entry.get("link", "")
        identity = (
            "id:" + entry["id"] if entry.get("id") else
            "link:" + link if link else
            "fallback:" + json.dumps([title, published], ensure_ascii=False)
        )
        articles.append({
            "fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
            "title": title, "summary": clean(entry.get("summary", "")),
            "published": published, "link": link,
        })
    return articles


def message(event_id: str, source: str, article: dict, matches: list[dict]) -> str:
    def bounded(value: str, size: int) -> str:
        encoded = value.encode()
        return value if len(encoded) <= size else encoded[:size - 3].decode("utf-8", errors="ignore") + "…"

    rules = "；".join(f"{m['name']}（{', '.join(m['keywords'])}）" for m in matches)
    return (
        f"RSS 命中提醒\n事件：{event_id}\n标题：{bounded(article['title'], 1500)}\n"
        f"来源：{bounded(source, 400)}\n规则及关键词：{bounded(rules, 2500)}\n"
        f"发布时间：{bounded(article['published'] or '未提供', 200)}\n"
        f"摘要：{article['summary'][:300]}\n原文：{bounded(article['link'], 2048)}"
    )

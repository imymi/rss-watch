import json

import pytest

from rss_watch.domain import clean, match, message, parse_feed

from .conftest import rss


def rule(includes=("AI",), excludes=(), mode="any"):
    return {"includes": json.dumps(includes), "excludes": json.dumps(excludes), "mode": mode}


@pytest.mark.parametrize("title,summary,expected", [
    ("ＡＩ 新动态", "", ["AI"]),
    ("<b>ai</b> 技术", "", ["AI"]),
    ("技术", "AI 内容", []),
    ("A", "I", []),
    ("中文", "", []),
])
def test_normalization(title, summary, expected):
    assert match(rule(), title, summary) == expected


def test_title_only_all_and_exclusion():
    assert match(rule(["AI", "研究"], mode="all"), "AI 研究", "") == ["AI", "研究"]
    assert match(rule(["AI", "研究"], mode="all"), "AI", "研究") == []
    assert match(rule(["AI"], ["广告"]), "AI", "广告") == ["AI"]
    assert match(rule(["AI"], ["广告"]), "AI 广告", "") == []
    assert clean("<p>你好&nbsp;世界</p><script>secret</script>") == "你好 世界"


@pytest.mark.parametrize("title,summary,expected", [
    ("Delux 更换 IP", "limitednumberofourVPSservicestoday", []),
    ("Delux 更换 IP", "BERO VPS", []),
    ("", "bero", []),
    ("BERO 优惠", "", ["bero"]),
    ("ＢＥＲＯ 优惠", "", ["bero"]),
    ("berostar", "", ["bero"]),
    ("numberof", "", ["bero"]),
])
def test_title_only_preserves_substring_matching(title, summary, expected):
    assert match(rule(["bero"]), title, summary) == expected


def test_rss_atom_and_missing_fields():
    entries = parse_feed(rss(("1", "ＡＩ 消息")))
    assert entries[0]["title"] == "AI 消息"
    assert entries[0]["published"] == ""
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>
      <id>feed-id</id><updated>2026-01-01T00:00:00Z</updated><entry><id>one</id>
      <title>AI</title><summary type="html">&lt;b&gt;News&lt;/b&gt;</summary></entry></feed>'''
    assert parse_feed(atom)[0]["summary"] == "News"
    empty_item = b'<rss version="2.0"><channel><item/></channel></rss>'
    assert parse_feed(empty_item)[0]["title"] == ""
    assert parse_feed(rss()) == []


@pytest.mark.parametrize("body", [b"not XML", b"<html><body>login</body></html>",
                                    b"<rss version='2.0'><channel><item>"])
def test_invalid_feed(body):
    with pytest.raises(ValueError):
        parse_feed(body)


def test_identity_fallback_and_update():
    assert parse_feed(rss(("1", "old")))[0]["fingerprint"] == \
           parse_feed(rss(("1", "new")))[0]["fingerprint"]
    link_feed = b'<rss version="2.0"><channel><item><title>A</title><link>https://x/a</link></item></channel></rss>'
    assert parse_feed(link_feed)[0]["fingerprint"] == \
           parse_feed(link_feed.replace(b">A<", b">B<"))[0]["fingerprint"]
    fallback = b'<rss version="2.0"><channel><item><title>A</title></item></channel></rss>'
    assert parse_feed(fallback)[0]["fingerprint"] != \
           parse_feed(fallback.replace(b">A<", b">B<"))[0]["fingerprint"]


def test_message_limit_preserves_identity_and_link():
    article = {"title": "中文" * 10000, "summary": "摘" * 1000,
               "published": "", "link": "https://example.com/story"}
    text = message("stable-event", "源", article, [{"name": "rule", "keywords": ["中文"]}])
    assert len(text.encode()) <= 10240
    assert "stable-event" in text and text.endswith(article["link"])
    for label in ("来源：", "规则及关键词：", "发布时间：", "摘要："):
        assert label in text


def test_response_body_cannot_be_opened_as_local_filename(tmp_path):
    path = tmp_path / "private.xml"
    path.write_bytes(rss(("secret", "local content")))
    with pytest.raises(ValueError):
        parse_feed(str(path).encode())

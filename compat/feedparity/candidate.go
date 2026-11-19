package feedparity

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	stdhtml "html"
	"net/url"
	"strings"
	"unicode"

	"github.com/mmcdole/gofeed"
	"golang.org/x/net/html"
	"golang.org/x/text/unicode/norm"
)

type article struct {
	Title       string `json:"title"`
	Summary     string `json:"summary"`
	Published   string `json:"published"`
	Link        string `json:"link"`
	Fingerprint string `json:"fingerprint"`
}

// parseCandidate tests the universal gofeed model plus final-response URL
// resolution. Identity and display fields are compared against Python.
func parseCandidate(body []byte, responseURL string) ([]article, error) {
	base, err := url.Parse(responseURL)
	if err != nil {
		return nil, err
	}
	feed, err := gofeed.NewParser().Parse(bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	articles := make([]article, 0, len(feed.Items))
	for _, item := range feed.Items {
		published := item.Published
		if published == "" {
			published = item.Updated
		}
		entry := article{
			Title:     cleanText(item.Title),
			Summary:   cleanText(item.Description),
			Published: published,
			Link:      resolveLink(base, item.Link),
		}
		entry.Fingerprint = fingerprint(item.GUID, entry)
		articles = append(articles, entry)
	}
	return articles, nil
}

func resolveLink(base *url.URL, link string) string {
	if link == "" {
		return ""
	}
	ref, err := url.Parse(link)
	if err != nil || ref.IsAbs() {
		return link
	}
	return base.ResolveReference(ref).String()
}

func cleanText(input string) string {
	tokenizer := html.NewTokenizer(strings.NewReader(input))
	var plain strings.Builder
	hidden := 0
	for {
		kind := tokenizer.Next()
		switch kind {
		case html.ErrorToken:
			text := norm.NFKC.String(stdhtml.UnescapeString(plain.String()))
			return strings.Join(strings.FieldsFunc(text, func(r rune) bool {
				return unicode.IsSpace(r) || ('\x1c' <= r && r <= '\x1f')
			}), " ")
		case html.StartTagToken, html.EndTagToken, html.SelfClosingTagToken:
			tag, _ := tokenizer.TagName()
			switch string(tag) {
			case "script", "style":
				if kind == html.EndTagToken {
					hidden = max(0, hidden-1)
				} else if kind == html.StartTagToken {
					hidden++
				}
			case "br":
				if kind != html.EndTagToken {
					plain.WriteByte(' ')
				}
			case "p", "div", "li", "tr":
				plain.WriteByte(' ')
			}
		case html.TextToken:
			if hidden == 0 {
				plain.Write(tokenizer.Text())
			}
		}
	}
}

func fingerprint(id string, entry article) string {
	var identity string
	switch {
	case id != "":
		identity = "id:" + id
	case entry.Link != "":
		identity = "link:" + entry.Link
	default:
		identity = "fallback:[" + pythonJSONString(entry.Title) + ", " +
			pythonJSONString(entry.Published) + "]"
	}
	sum := sha256.Sum256([]byte(identity))
	return hex.EncodeToString(sum[:])
}

// Match json.dumps(value, ensure_ascii=False), including literal U+2028/U+2029.
func pythonJSONString(value string) string {
	const hexDigits = "0123456789abcdef"
	var result strings.Builder
	result.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"', '\\':
			result.WriteByte('\\')
			result.WriteRune(r)
		case '\b':
			result.WriteString(`\b`)
		case '\f':
			result.WriteString(`\f`)
		case '\n':
			result.WriteString(`\n`)
		case '\r':
			result.WriteString(`\r`)
		case '\t':
			result.WriteString(`\t`)
		default:
			if r < ' ' {
				result.WriteString(`\u00`)
				result.WriteByte(hexDigits[r>>4])
				result.WriteByte(hexDigits[r&15])
			} else {
				result.WriteRune(r)
			}
		}
	}
	result.WriteByte('"')
	return result.String()
}

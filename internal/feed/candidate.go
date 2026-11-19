package feed

import (
	"crypto/sha256"
	"encoding/hex"
	stdhtml "html"
	"strings"
	"unicode"

	"golang.org/x/net/html"
	"golang.org/x/text/unicode/norm"
)

// Article is a normalized feed entry with the v0.2 article identity.
type Article struct {
	Title       string `json:"title"`
	Summary     string `json:"summary"`
	Published   string `json:"published"`
	Link        string `json:"link"`
	Fingerprint string `json:"fingerprint"`
}

// Clean normalizes the text shown by the monitor and used in title matching.
func Clean(input string) string {
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

func fingerprint(id string, entry Article) string {
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

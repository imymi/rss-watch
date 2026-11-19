package feed

import (
	"bytes"
	"encoding/base64"
	"encoding/xml"
	"errors"
	"fmt"
	stdhtml "html"
	"io"
	"regexp"
	"strings"
	"unicode/utf8"

	"golang.org/x/net/html/charset"
	"golang.org/x/text/encoding/unicode"
)

const (
	atom10 = "http://www.w3.org/2005/Atom"
	atom03 = "http://purl.org/atom/ns#"
	rdf    = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
	rss09  = "http://my.netscape.com/rdf/simple/0.9/"
	rss10  = "http://purl.org/rss/1.0/"
	dc     = "http://purl.org/dc/elements/1.1/"
	dct    = "http://purl.org/dc/terms/"
)

// Parse reads the complete XML document before returning articles. A feed with a
// usable prefix and a broken suffix must never establish a baseline.
func Parse(body []byte, responseURL string) ([]Article, error) {
	d := feedDecoder(body)
	root, err := documentRoot(d)
	if err != nil {
		return nil, err
	}
	rootName := strings.ToLower(root.Name.Local)
	isAtom := rootName == "feed" && (root.Name.Space == "" || atomNamespace(root.Name.Space))
	isRSS := rootName == "rss" || (rootName == "rdf" && root.Name.Space == rdf)
	if !isAtom && !isRSS {
		return nil, errors.New("unsupported RSS/Atom root")
	}

	articles := []Article{}
	if err := readFeedChildren(d, elementBase(responseURL, root), rootName, root.Name.Space, &articles); err != nil {
		return nil, err
	}
	for {
		token, err := d.Token()
		if err == io.EOF {
			return articles, nil
		}
		if err != nil {
			return nil, err
		}
		switch token := token.(type) {
		case xml.Comment, xml.ProcInst:
			continue
		case xml.CharData:
			if xmlSpace(string(token)) {
				continue
			}
		}
		return nil, errors.New("data after RSS/Atom root")
	}
}

func feedDecoder(body []byte) *xml.Decoder {
	body = bytes.TrimPrefix(body, []byte{0xef, 0xbb, 0xbf})
	var endian unicode.Endianness
	var utf16 bool
	switch {
	case bytes.HasPrefix(body, []byte{0xfe, 0xff}):
		endian, utf16, body = unicode.BigEndian, true, body[2:]
	case bytes.HasPrefix(body, []byte{0xff, 0xfe}):
		endian, utf16, body = unicode.LittleEndian, true, body[2:]
	case bytes.HasPrefix(body, []byte{0, '<', 0, '?'}):
		endian, utf16 = unicode.BigEndian, true
	case bytes.HasPrefix(body, []byte{'<', 0, '?', 0}):
		endian, utf16 = unicode.LittleEndian, true
	}
	var input io.Reader = bytes.NewReader(body)
	if utf16 {
		input = unicode.UTF16(endian, unicode.IgnoreBOM).NewDecoder().Reader(input)
	}
	d := xml.NewDecoder(input)
	d.CharsetReader = func(label string, input io.Reader) (io.Reader, error) {
		if utf16 && strings.HasPrefix(strings.ToLower(label), "utf-16") {
			return input, nil
		}
		return charset.NewReaderLabel(label, input)
	}
	return d
}

func documentRoot(d *xml.Decoder) (xml.StartElement, error) {
	for {
		token, err := d.Token()
		if err != nil {
			return xml.StartElement{}, err
		}
		switch token := token.(type) {
		case xml.StartElement:
			return token, nil
		case xml.CharData:
			if !xmlSpace(string(token)) {
				return xml.StartElement{}, errors.New("text before RSS/Atom root")
			}
		}
	}
}

func xmlSpace(value string) bool {
	return strings.Trim(value, " \t\r\n") == ""
}

func readFeedChildren(d *xml.Decoder, base, parent, native string, articles *[]Article) error {
	for {
		token, err := d.Token()
		if err != nil {
			return err
		}
		switch token := token.(type) {
		case xml.StartElement:
			name := strings.ToLower(token.Name.Local)
			space := token.Name.Space
			switch {
			case parent != "feed" && parent != "channel" && name == "channel" && rssNamespace(space, native):
				if err := readFeedChildren(d, elementBase(base, token), "channel", native, articles); err != nil {
					return err
				}
			case (parent == "feed" && name == "entry" && (space == "" || atomNamespace(space))) ||
				(parent != "feed" && name == "item" && rssNamespace(space, native)):
				entry, err := readEntry(d, base, token, native)
				if err != nil {
					return err
				}
				*articles = append(*articles, entry)
			default:
				if err := d.Skip(); err != nil {
					return err
				}
			}
		case xml.EndElement:
			return nil
		}
	}
}

func rssNamespace(space, native string) bool {
	return space == "" || space == rss09 || space == rss10 || (space == native && native != rdf)
}

func atomNamespace(space string) bool {
	return space == atom10 || space == atom03
}

type entryFields struct {
	id, link, title, summary, published, updated  string
	hasLink, hasSummary, hasPublished, hasContent bool
}

func readEntry(d *xml.Decoder, base string, start xml.StartElement, native string) (Article, error) {
	base = elementBase(base, start)
	fields := entryFields{id: attribute(start, rdf, "about")}
	for {
		token, err := d.Token()
		if err != nil {
			return Article{}, err
		}
		switch token := token.(type) {
		case xml.StartElement:
			field := fieldName(token.Name, native)
			if field == "" {
				if err := d.Skip(); err != nil {
					return Article{}, err
				}
				continue
			}
			value, err := readFieldText(d, field == "title" || field == "summary" || field == "content")
			if err != nil {
				return Article{}, err
			}
			fields.set(field, value, elementBase(base, token), token)
		case xml.EndElement:
			if !fields.hasPublished {
				fields.published = fields.updated
			}
			entry := Article{
				Title:     Clean(fields.title),
				Summary:   Clean(fields.summary),
				Published: fields.published,
				Link:      fields.link,
			}
			entry.Fingerprint = fingerprint(fields.id, entry)
			return entry, nil
		}
	}
}

func fieldName(name xml.Name, native string) string {
	local := strings.ToLower(name.Local)
	switch name.Space {
	case dc:
		switch local {
		case "title":
			return "title"
		case "description":
			return "summary"
		case "date":
			return "updated"
		}
		return ""
	case dct:
		switch local {
		case "issued":
			return "published"
		case "modified":
			return "updated"
		}
		return ""
	case "http://purl.org/rss/1.0/modules/content/":
		if local == "encoded" {
			return "content"
		}
		return ""
	}
	if !rssNamespace(name.Space, native) && !atomNamespace(name.Space) {
		return ""
	}
	switch local {
	case "title", "link", "content", "summary":
		return local
	case "id", "guid":
		return "id"
	case "description", "abstract":
		return "summary"
	case "published", "pubdate", "issued":
		return "published"
	case "updated", "modified", "lastbuilddate":
		return "updated"
	case "fullitem", "body":
		return "content"
	}
	return ""
}

func (f *entryFields) set(field, value, base string, element xml.StartElement) {
	switch field {
	case "id":
		permalink, present := lookupAttr(element, "", "ispermalink")
		if !present || permalink == "true" {
			value = resolveLink(base, value)
			if !f.hasLink {
				f.link, f.hasLink = value, true
			}
		}
		f.id = value
	case "link":
		href, present := lookupAttr(element, "", "href")
		for _, key := range []string{"url", "uri"} {
			if alternate := attribute(element, "", key); alternate != "" {
				href, present = alternate, true
				break
			}
		}
		if !present {
			f.link, f.hasLink = queryEntity.ReplaceAllString(strings.ReplaceAll(resolveLink(base, value), "&amp;", "&"), "&${1}"), true
			return
		}
		rel, hasRel := lookupAttr(element, "", "rel")
		if (!hasRel || rel == "alternate") && isHTML(contentType(element, "text/html")) {
			if href == "" {
				href = base
			}
			f.link, f.hasLink = resolveLink(base, href), true
		}
	case "title":
		if f.title == "" {
			f.title = decodeContent(value, element)
		}
	case "summary":
		if f.hasSummary && !f.hasContent {
			f.hasContent = true
		} else {
			f.summary, f.hasSummary = decodeContent(value, element), true
		}
	case "content":
		f.hasContent = true
		kind := contentType(element, "text/plain")
		if !f.hasSummary && (isHTML(kind) || kind == "text/plain") {
			f.summary, f.hasSummary = decodeContent(value, element), true
		}
	case "published":
		f.published, f.hasPublished = value, true
	case "updated":
		f.updated = value
	}
}

var queryEntity = regexp.MustCompile(`&([A-Za-z0-9_]+);`)

func contentType(element xml.StartElement, defaultType string) string {
	kind, present := lookupAttr(element, "", "type")
	if !present {
		return defaultType
	}
	switch kind {
	case "html":
		return "text/html"
	case "xhtml":
		return "application/xhtml+xml"
	case "text":
		return "text/plain"
	}
	return kind
}

func isHTML(kind string) bool {
	return kind == "text/html" || kind == "application/xhtml+xml"
}

func decodeContent(value string, element xml.StartElement) string {
	kind := contentType(element, "text/plain")
	mode := attribute(element, "", "mode")
	if mode == "base64" || (mode != "xml" && !strings.HasPrefix(kind, "text/") &&
		!strings.HasSuffix(kind, "+xml") && !strings.HasSuffix(kind, "/xml")) {
		// Python's base64 decoder ignores whitespace within the payload.
		encoded := strings.Join(strings.Fields(value), "")
		if decoded, err := base64.StdEncoding.DecodeString(encoded); err == nil && utf8.Valid(decoded) {
			return string(decoded)
		}
	}
	return value
}

func readFieldText(d *xml.Decoder, preserveMarkup bool) (string, error) {
	var text, markup strings.Builder
	depth := 0
	hasMarkup := false
	for {
		token, err := d.Token()
		if err != nil {
			return "", err
		}
		switch token := token.(type) {
		case xml.StartElement:
			depth++
			if preserveMarkup {
				hasMarkup = true
				fmt.Fprintf(&markup, "<%s>", token.Name.Local)
			}
		case xml.EndElement:
			if depth == 0 {
				if hasMarkup {
					return strings.TrimSpace(markup.String()), nil
				}
				return strings.TrimSpace(text.String()), nil
			}
			depth--
			if preserveMarkup {
				fmt.Fprintf(&markup, "</%s>", token.Name.Local)
			}
		case xml.CharData:
			text.Write(token)
			if preserveMarkup {
				markup.WriteString(stdhtml.EscapeString(string(token)))
			}
		}
	}
}

func attribute(element xml.StartElement, space, name string) string {
	value, _ := lookupAttr(element, space, name)
	return value
}

func lookupAttr(element xml.StartElement, space, name string) (string, bool) {
	for _, attr := range element.Attr {
		if attr.Name.Space == space && strings.EqualFold(attr.Name.Local, name) {
			return attr.Value, true
		}
	}
	return "", false
}

func elementBase(base string, element xml.StartElement) string {
	value, present := lookupAttr(element, "http://www.w3.org/XML/1998/namespace", "base")
	if !present {
		value = attribute(element, "", "base")
	}
	if resolved := resolveLink(base, value); resolved != "" {
		return resolved
	}
	return base
}

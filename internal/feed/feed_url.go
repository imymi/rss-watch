package feed

import (
	"net/url"
	"regexp"
	"strings"
)

var extraSchemeSlashes = regexp.MustCompile(`^([A-Za-z][A-Za-z0-9+.-]*://)/+`)

// Fingerprints depend on the original URL spelling. In particular, url.URL.String
// escapes spaces and non-ASCII path characters that Python's urljoin retains.
func resolveLink(base, link string) string {
	link = extraSchemeSlashes.ReplaceAllString(link, "${1}")
	if base == "" || link == "" {
		return link
	}
	origin, err := url.Parse(base)
	if err != nil {
		return ""
	}
	ref, err := url.Parse(link)
	if err != nil || (ref.Scheme != "" && ref.Scheme != origin.Scheme) {
		return link
	}
	if ref.Host != "" {
		_, address, _ := strings.Cut(link, "//")
		return origin.Scheme + "://" + address
	}

	path := rawPath(ref)
	query := ref.RawQuery
	if path == "" {
		path = rawPath(origin)
		if query == "" {
			query = origin.RawQuery
		}
	} else {
		path = joinURLPath(rawPath(origin), path)
	}
	fragment := ref.RawFragment
	if fragment == "" {
		fragment = ref.Fragment
	}

	result := path
	if origin.Host != "" {
		host := origin.Host
		if origin.User != nil {
			host = origin.User.String() + "@" + host
		}
		result = "//" + host + path
	}
	if origin.Scheme != "" {
		result = origin.Scheme + ":" + result
	}
	if query != "" {
		result += "?" + query
	}
	if fragment != "" {
		result += "#" + fragment
	}
	return result
}

func rawPath(value *url.URL) string {
	if value.Opaque != "" {
		return value.Opaque
	}
	if value.RawPath != "" {
		return value.RawPath
	}
	return value.Path
}

func joinURLPath(base, ref string) string {
	relative := !strings.HasPrefix(ref, "/")
	if relative {
		parent := base[:strings.LastIndex(base, "/")+1]
		ref = parent + ref
	}
	parts := strings.Split(strings.TrimPrefix(ref, "/"), "/")
	path := make([]string, 0, len(parts))
	for index, part := range parts {
		switch part {
		case ".":
		case "..":
			if len(path) > 0 {
				path = path[:len(path)-1]
			}
		default:
			if part != "" || !relative || index == len(parts)-1 {
				path = append(path, part)
			}
		}
	}
	if last := parts[len(parts)-1]; last == "." || last == ".." {
		path = append(path, "")
	}
	return "/" + strings.Join(path, "/")
}

package feed

import (
	"slices"
	"strings"

	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

func FoldTitle(title string) string { return cases.Fold().String(Clean(title)) }

func NormalizeWord(word string) string {
	return cases.Fold().String(strings.TrimSpace(norm.NFKC.String(word)))
}

// MatchWords reports the original keywords that match the folded title.
func MatchWords(title, mode string, includes, excludes []string) []string {
	contains := func(word string) bool { return strings.Contains(title, NormalizeWord(word)) }
	hits := []string{}
	if slices.ContainsFunc(excludes, contains) {
		return hits
	}
	for _, word := range includes {
		if contains(word) {
			hits = append(hits, word)
		}
	}
	if mode != "any" && len(hits) != len(includes) {
		return []string{}
	}
	return hits
}

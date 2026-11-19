package feedparity

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"rss-watch/internal/feed"
)

const responseURL = "https://feeds.example.test/final/updates.xml?from=redirect"

func TestPythonParity(t *testing.T) {
	compareFixtures(t, "testdata/*.*", nil)
}

func TestRealFeedParity(t *testing.T) {
	body, err := os.ReadFile("testdata/real/response-urls.json")
	if err != nil {
		t.Fatal(err)
	}
	var origins map[string]string
	if err := json.Unmarshal(body, &origins); err != nil {
		t.Fatal(err)
	}
	compareFixtures(t, "testdata/real/*.xml", origins)
}

func compareFixtures(t *testing.T, pattern string, origins map[string]string) {
	t.Helper()
	paths, err := filepath.Glob(pattern)
	if err != nil || len(paths) == 0 {
		t.Fatalf("load fixtures: %v (%d files)", err, len(paths))
	}
	python := os.Getenv("FEED_PARITY_PYTHON")
	if python == "" {
		python = "python3"
	}
	args := []string{"-B", "python_oracle.py"}
	samples := make(map[string]string, len(paths))
	for _, path := range paths {
		base := responseURL
		if origins != nil {
			base = origins[filepath.Base(path)]
			if base == "" {
				t.Fatalf("missing anonymized response URL for %s", path)
			}
		}
		samples[path] = base
		args = append(args, path, base)
	}
	cmd := exec.CommandContext(t.Context(), python, args...)
	output, err := cmd.Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			t.Fatalf("Python oracle failed: %v\n%s", err, exit.Stderr)
		}
		t.Fatalf("run Python oracle: %v", err)
	}
	var reference map[string][]feed.Article
	if err := json.Unmarshal(output, &reference); err != nil {
		t.Fatalf("decode Python oracle: %v", err)
	}

	type stats struct {
		fixtures, exact, acceptance, count, fingerprint, fields int
		items, fingerprints                                     int
	}
	var total stats
	for _, path := range paths {
		name := filepath.Base(path)
		t.Run(name, func(t *testing.T) {
			total.fixtures++
			expected, ok := reference[name]
			if !ok {
				t.Fatalf("Python oracle omitted %s", name)
			}
			body, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			got, parseErr := feed.Parse(body, samples[path])
			switch {
			case (parseErr != nil) != (expected == nil):
				total.acceptance++
				t.Errorf("acceptance differs: Python accepted=%v, Go error=%v", expected != nil, parseErr)
				return
			case expected == nil:
				total.exact++
				return
			case len(expected) != len(got):
				total.count++
				t.Errorf("article count differs: Python=%d, Go=%d", len(expected), len(got))
				return
			}

			fingerprintDiffers, fieldsDiffer := false, false
			for index, want := range expected {
				actual := got[index]
				total.items++
				if actual.Fingerprint != want.Fingerprint {
					fingerprintDiffers = true
					total.fingerprints++
					t.Errorf("item %d fingerprint: Python=%s Go=%s", index+1, want.Fingerprint, actual.Fingerprint)
				}
				for _, field := range []struct {
					name, want, actual string
				}{
					{"title", want.Title, actual.Title},
					{"summary", want.Summary, actual.Summary},
					{"published", want.Published, actual.Published},
					{"link", want.Link, actual.Link},
				} {
					if field.want != field.actual {
						fieldsDiffer = true
						t.Errorf("item %d %s: Python=%q Go=%q", index+1, field.name, field.want, field.actual)
					}
				}
			}
			if fingerprintDiffers {
				total.fingerprint++
			}
			if fieldsDiffer {
				total.fields++
			}
			if !t.Failed() {
				total.exact++
			}
		})
	}
	t.Logf("%d/%d fixtures match; %d acceptance differences, %d count differences, "+
		"%d fingerprint differences across %d fixtures (%d comparable articles), field differences in %d fixtures",
		total.exact, total.fixtures, total.acceptance, total.count,
		total.fingerprints, total.fingerprint, total.items, total.fields)
}

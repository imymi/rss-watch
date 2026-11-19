// Package buildinfo supplies the release version for the Go application.
package buildinfo

import (
	_ "embed"
	"strings"
)

//go:embed VERSION
var versionFile string

// Version is the Go release version without the tag's leading v.
var Version = strings.TrimSpace(versionFile)

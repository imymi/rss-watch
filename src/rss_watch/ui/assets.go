package ui

import "embed"

// Assets contains the same templates and static assets served by the Python web app.
//
//go:embed page.html login.html static
var Assets embed.FS

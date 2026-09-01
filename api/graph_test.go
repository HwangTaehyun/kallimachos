package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Why this file exists
//
//	`GET /api/graph` serves about 6MB of JSON through ServeContent.  That much is right ——
//	If-Modified-Since makes a second visit a 304.  But **without Cache-Control a browser will
//	not even revalidate**: for a response carrying neither Expires nor Cache-Control,
//	RFC 9111 §4.2.2 permits inventing a lifetime from Last-Modified.
//
//	Measured 2026-09-01: after re-exporting the graph, with the server holding 21 English
//	cluster labels, a fresh window still showed the old Korean ones.  No conditional request went out.
//
//	The header raises no error.  Delete it and the screen just goes quietly stale days later,
//	so without a check the next person removes "a line that looks unnecessary".
func TestGraphRevalidates(t *testing.T) {
	vault := t.TempDir()
	dir := filepath.Join(vault, ".obsidian", "plugins", "kal-galaxy")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"nodes":[],"links":[]}`)
	p := filepath.Join(dir, "kal-graph.json")
	if err := os.WriteFile(p, body, 0o644); err != nil {
		t.Fatal(err)
	}
	mtime := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	if err := os.Chtimes(p, mtime, mtime); err != nil {
		t.Fatal(err)
	}

	s := &Server{vault: vault, vaultName: "demo"}

	//  ① the first request —— a body arrives, with the revalidation instruction attached
	w := httptest.NewRecorder()
	s.graph(w, httptest.NewRequest(http.MethodGet, "/api/graph", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("the first request gave %d (body %q)", w.Code, w.Body.String()[:min(80, w.Body.Len())])
	}
	if got := w.Header().Get("Cache-Control"); got != "no-cache" {
		t.Errorf("Cache-Control=%q —— without it a browser falls back to heuristic caching and "+
			"keeps using the old graph however often the export is re-run", got)
	}
	if got := w.Header().Get("X-Vault-Name"); got != "demo" {
		t.Errorf("X-Vault-Name=%q, expected %q —— the viewer cannot build obsidian:// links", got, "demo")
	}

	//  ② `no-cache` does not mean "do not store".  A conditional request must still be a 304 ——
	//     without checking this, the next person "hardens" it to `no-store` and resends 6MB every time.
	r := httptest.NewRequest(http.MethodGet, "/api/graph", nil)
	r.Header.Set("If-Modified-Since", mtime.Format(http.TimeFormat))
	w2 := httptest.NewRecorder()
	s.graph(w2, r)
	if w2.Code != http.StatusNotModified {
		t.Errorf("the conditional request gave %d —— it must be 304.  6MB is resent every time", w2.Code)
	}
	if w2.Body.Len() != 0 {
		t.Errorf("304 with a %d-byte body", w2.Body.Len())
	}
}

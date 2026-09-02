package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
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

// The graph is served from **KAL_HOME**, and the vault is only a fallback.
//
//	Before 2026-09-02 it was read from `<vault>/.obsidian/plugins/kal-galaxy/` and nowhere else,
//	which was this handler's only use of the vault mount —— the api container mounted a whole
//	vault to serve one file.  Nothing in the file needs to be there: it is built from LanceDB and
//	its document references are vault-relative.  Only the Obsidian plugin needs a copy inside a
//	vault, because a plugin cannot open a file outside its own.
//
//	Both arms are checked.  Keeping only the first would let the fallback rot and break every
//	installation that has not re-exported; keeping only the second is the state this replaced.
func TestGraphPrefersHomeOverVault(t *testing.T) {
	write := func(dir, body string) string {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		p := filepath.Join(dir, "kal-graph.json")
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	get := func(s *Server) (int, string) {
		w := httptest.NewRecorder()
		s.graph(w, httptest.NewRequest(http.MethodGet, "/api/graph", nil))
		return w.Code, w.Body.String()
	}

	//  ① KAL_HOME wins when both exist
	home, vault := t.TempDir(), t.TempDir()
	write(filepath.Join(home, "graph_export"), `{"source":"home"}`)
	write(filepath.Join(vault, ".obsidian", "plugins", "kal-galaxy"), `{"source":"vault"}`)
	if code, body := get(&Server{home: home, vault: vault, vaultName: "demo"}); code != 200 ||
		!strings.Contains(body, `"home"`) {
		t.Fatalf("KAL_HOME did not win: %d %q", code, body)
	}

	//  ② the vault still serves an installation that has not re-exported
	home2 := t.TempDir()
	if code, body := get(&Server{home: home2, vault: vault, vaultName: "demo"}); code != 200 ||
		!strings.Contains(body, `"vault"`) {
		t.Fatalf("the fallback did not serve: %d %q", code, body)
	}

	//  ③ neither —— a 404 that says what to do, not a 500
	if code, _ := get(&Server{home: t.TempDir(), vault: t.TempDir(), vaultName: "demo"}); code != 404 {
		t.Fatalf("with no graph anywhere the status was %d, want 404", code)
	}

	//  ④ **the vault is not needed at all** once KAL_HOME has the file —— this is the property the
	//     change exists for: a viewer-only container does not have to mount a vault.
	if code, body := get(&Server{home: home, vault: "/nonexistent-vault", vaultName: "demo"}); code != 200 ||
		!strings.Contains(body, `"home"`) {
		t.Fatalf("serving needed the vault after all: %d %q", code, body)
	}
}

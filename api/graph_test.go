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
	//  The graph lives in KAL_HOME.  This test used to place it under
	//  `<vault>/.obsidian/plugins/kal-galaxy/`, and kept passing through the fallback that existed
	//  for one day; with the fallback gone it 404'd, which is the fallback's whole point being
	//  made by the test suite itself.
	home := t.TempDir()
	dir := filepath.Join(home, "graph_export")
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

	s := &Server{home: home, vaultName: "demo"}

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

	//  ② **the vault is never read.**  The fallback was removed on 2026-09-03: a second place to
	//     look is a second thing to keep true.  A graph sitting only in a vault must 404, so the
	//     message tells the operator to re-export rather than serving something stale.
	home2 := t.TempDir()
	if code, _ := get(&Server{home: home2, vault: vault, vaultName: "demo"}); code != 404 {
		t.Fatalf("a graph in the vault was served: %d, want 404", code)
	}

	//  ③ neither —— a 404 that says what to do, not a 500
	if code, _ := get(&Server{home: t.TempDir(), vault: t.TempDir(), vaultName: "demo"}); code != 404 {
		t.Fatalf("with no graph anywhere the status was %d, want 404", code)
	}

	//  ④ **the vault is not needed at all** —— the property the change exists for: a viewer-only
	//     container does not mount one, and nothing here reaches for it.
	if code, body := get(&Server{home: home, vault: "/nonexistent-vault", vaultName: "demo"}); code != 200 ||
		!strings.Contains(body, `"home"`) {
		t.Fatalf("serving needed the vault after all: %d %q", code, body)
	}
}

// A step that writes the DB must not run when there are no notes to read.
//
//	`up-viewer` mounts no vault, and an empty KAL_VAULT falls through to ~/.kal/config.json —— a
//	**host** path that does not exist inside the container.  "Rebuild knowledge DB" would then
//	overwrite every table with empty rowsets and "Incremental sync" would call all 1,115 indexed
//	documents deleted.  One click, no confirmation.
//
//	Both directions: a vault with notes must still be allowed, or the guard would block the
//	normal case and be removed the first time someone hit it.
func TestCountMarkdownGatesDestructiveRuns(t *testing.T) {
	empty := t.TempDir()
	if n, err := countMarkdown(empty, 1); err != nil || n != 0 {
		t.Fatalf("an empty folder counted %d (err %v)", n, err)
	}
	if n, _ := countMarkdown("", 1); n != 0 {
		t.Fatalf("an empty path counted %d", n)
	}
	if n, _ := countMarkdown(filepath.Join(empty, "nope"), 1); n != 0 {
		t.Fatalf("a missing folder counted %d", n)
	}

	//  Dot-directories are not notes —— .obsidian holds plugin data, and counting it would let a
	//  vault that has lost every note still look populated.
	hidden := t.TempDir()
	if err := os.MkdirAll(filepath.Join(hidden, ".obsidian"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hidden, ".obsidian", "x.md"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if n, _ := countMarkdown(hidden, 1); n != 0 {
		t.Fatalf("a note inside a dot-directory counted %d", n)
	}

	//  ⚠ It must **under**-count, not over-count.  The indexer drops every generated `index.md`
	//     and every body under 60 characters, so counting those made the guard pass on a vault the
	//     indexer reads nothing from —— and the rebuild then emptied the DB (2026-09-04).
	onlyIndex := t.TempDir()
	if err := os.MkdirAll(filepath.Join(onlyIndex, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	//  Big enough to clear the size floor, so only the **name** rule can exclude them —— otherwise
	//  the two conditions cannot be told apart and deleting either leaves the test green.
	for _, rel := range []string{"index.md", "sub/index.md"} {
		if err := os.WriteFile(filepath.Join(onlyIndex, rel),
			[]byte("---\ntitle: i\n---\n"+strings.Repeat("생성된 색인 ", 40)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(onlyIndex, "stub.md"), []byte("tiny\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if n, _ := countMarkdown(onlyIndex, 1); n != 0 {
		t.Fatalf("a vault of index.md and stubs counted %d — the indexer reads none of them", n)
	}

	//  The other direction —— a real vault, including one where the notes are nested.
	full := t.TempDir()
	if err := os.MkdirAll(filepath.Join(full, "a", "b"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(full, "a", "b", "deep.md"),
		[]byte("---\ntitle: d\n---\n"+strings.Repeat("본문 ", 40)), 0o644); err != nil {
		t.Fatal(err)
	}
	if n, err := countMarkdown(full, 1); err != nil || n != 1 {
		t.Fatalf("a nested note counted %d (err %v)", n, err)
	}
}

// The **handler** must refuse a DB-writing step when the vault has no notes.
//
//	A first version of this check only exercised `countMarkdown()`.  Removing the guard from
//	`startRun` left it green —— the same "green with the production call site deleted" shape this
//	repository keeps paying for.  This one posts to the handler.
func TestStartRunRefusesDestructiveWithNoNotes(t *testing.T) {
	post := func(s *Server, step string) (int, string) {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, "/api/runs",
			strings.NewReader(`{"step":"`+step+`"}`))
		r.Header.Set("Content-Type", "application/json")
		s.startRun(w, r)
		return w.Code, w.Body.String()
	}
	mk := func(vault string) *Server {
		return &Server{
			home:  t.TempDir(),
			vault: vault,
			steps: map[string]Step{
				"index":  {ID: "index", WritesDB: true},
				"verify": {ID: "verify"},
			},
			subs: map[string]map[chan string]struct{}{},
		}
	}

	//  ① no notes → 412, and the message has to say what would happen
	code, body := post(mk(t.TempDir()), "index")
	if code != 412 {
		t.Fatalf("a DB-writing step with an empty vault returned %d, want 412 (body %q)", code, body)
	}
	if !strings.Contains(body, "empty it") {
		t.Errorf("the refusal does not say the DB would be emptied: %q", body)
	}

	//  ② a vault with a note → the guard does not fire.  Without this the guard could block
	//     everything and nobody would notice until it was removed.
	//  A realistic note —— `countMarkdown` deliberately ignores anything the indexer would drop,
	//  so a one-byte fixture is not a populated vault.
	full := t.TempDir()
	if err := os.WriteFile(filepath.Join(full, "a.md"),
		[]byte("---\ntitle: a\n---\n"+strings.Repeat("본문 ", 40)), 0o644); err != nil {
		t.Fatal(err)
	}
	if code, body := post(mk(full), "index"); code == 412 {
		t.Fatalf("a populated vault was refused: %q", body)
	}
}

// `envOr` treats an empty value as unset, and viewer mode depends on that being deliberate.
//
//	`docker-compose.viewer.yml` sets `KAL_VAULT: ""` and `KAL_VAULT_NAME: ""` on purpose, and its
//	comment says an empty one is "the truthful reading — not a path that looks mounted and is
//	not."  But `envOr` turns those empties into `/vault` and (via filepath.Base) `"vault"`, so the
//	412 message names a path that is not mounted and `X-Vault-Name` emits a deep link for a vault
//	that does not exist.  The rule had no test at all: inverting it left `go test` green.
//
//	This pins the behaviour rather than changing it — the default is load-bearing for every other
//	stack — so the divergence is recorded where the next person will see it.
func TestEnvOrTreatsEmptyAsUnset(t *testing.T) {
	t.Setenv("KAL_TEST_ENVOR", "")
	if got := envOr("KAL_TEST_ENVOR", "fallback"); got != "fallback" {
		t.Fatalf("an empty value did not fall back: %q", got)
	}
	t.Setenv("KAL_TEST_ENVOR", "set")
	if got := envOr("KAL_TEST_ENVOR", "fallback"); got != "set" {
		t.Fatalf("a set value was overridden: %q", got)
	}
	os.Unsetenv("KAL_TEST_ENVOR")
	if got := envOr("KAL_TEST_ENVOR", "fallback"); got != "fallback" {
		t.Fatalf("an unset value did not fall back: %q", got)
	}

	//  The consequence viewer mode inherits: an explicitly empty KAL_VAULT still reads as /vault.
	//  If that ever changes, viewer mode's messages change, and this test says where to look.
	t.Setenv("KAL_VAULT", "")
	if got := envOr("KAL_VAULT", "/vault"); got != "/vault" {
		t.Fatalf("KAL_VAULT=\"\" no longer resolves to /vault: %q — viewer mode's messages change", got)
	}
}

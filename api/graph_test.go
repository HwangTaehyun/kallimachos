package main

import (
	"context"
	"fmt"
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
	//  The count now comes from `schema_v3.indexable_count`, so this exercises the real rule
	//  rather than a Go restatement of it —— which is the whole point: the restatement is what
	//  disagreed.  It needs the project venv; `go test` in this repository runs after `uv sync`,
	//  and a missing one is a broken checkout, not a reason to pass quietly.
	//
	//	⚠ **Mutate the Python and you must pass `-count=1`.**  Go's test cache keys on Go inputs,
	//	  so editing `schema_v3.py` leaves it valid and the run prints `ok (cached)`.  That is how
	//	  the first mutation sweep of this test reported "the check does not fire" when it does
	//	  (2026-09-04) —— a cached pass and a real pass are indistinguishable on screen.
	repo := ".."
	py := filepath.Join(repo, ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Fatalf("the project venv is missing (%v) —— run `uv sync`; skipping would make this "+
			"check indistinguishable from a passing one", err)
	}
	srv := &Server{python: py, src: filepath.Join(repo, "src")}
	countMarkdown := func(root string, limit int) (int, error) {
		return srv.indexableCount(context.Background(), root, limit)
	}

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

	//  ⚠ **F5** —— the shape that actually occurs, and the one a size floor cannot see.  An OKF
	//     page carries ~80 bytes of frontmatter, so a document with a three-character body is a
	//     66-byte file: over any 60-**byte** floor, under the real 60-**character** body floor.
	//     Measured 2026-09-04: three such files counted as 3 in Go and 0 in `scan_vault`, the
	//     guard passed, `index` ran, and every table was overwritten empty.
	fatFM := t.TempDir()
	for i := range 3 {
		if err := os.WriteFile(filepath.Join(fatFM, fmt.Sprintf("p%d.md", i)),
			[]byte("---\ntype: note\ntitle: page\nstatus: draft\ndescription: d\n---\nabc\n"),
			0o644); err != nil {
			t.Fatal(err)
		}
	}
	//  Proves the premise rather than assuming it —— if frontmatter ever shrinks below the old
	//  floor this case stops testing what it says it tests, silently.
	if fi, err := os.Stat(filepath.Join(fatFM, "p0.md")); err != nil || fi.Size() < 60 {
		t.Fatalf("the fixture no longer clears a 60-byte floor (%d bytes) — it tests nothing", fi.Size())
	}
	if n, err := countMarkdown(fatFM, 1); err != nil || n != 0 {
		t.Fatalf("a stub with fat frontmatter counted %d (err %v) — the indexer reads none of them", n, err)
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
	//  The guard now asks schema_v3 for the count, so the server needs the interpreter and src.
	//  Without them `indexableCount` errors and every vault looks empty —— which would make the
	//  "a populated vault is allowed" half pass for the wrong reason.
	py := filepath.Join("..", ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Fatalf("the project venv is missing (%v) —— run `uv sync`", err)
	}
	mk := func(vault string) *Server {
		return &Server{
			home:   t.TempDir(),
			python: py,
			src:    filepath.Join("..", "src"),
			vault:  vault,
			//  The fixtures carry the same `reads`/`writes` the real steps declare, because that
			//  is what the guard reads now.  A bare `{ID: "index", WritesDB: true}` stopped
			//  matching the moment the condition moved off WritesDB —— and a fixture that no
			//  longer trips the guard tests nothing while still passing.
			steps: map[string]Step{
				"index": {ID: "index", WritesDB: true,
					Reads: "vault **/*.md + ~/.kal/lr_kg.json", Writes: "documents · chunks · ix_* · lr_*"},
				//  ⚠ `extract` is the one that got through: it reads the vault and writes, but not
				//     the DB.  Keep it here or the hole reopens unnoticed.
				"extract": {ID: "extract", WritesDB: false,
					Reads: "vault **/*.md", Writes: "~/.kal/lr_kg.json"},
				"verify": {ID: "verify", Reads: "docs/**/*.md + LanceDB"},
			},
			subs: map[string]map[chan string]struct{}{},
		}
	}

	//  ① no notes → 412, and the message has to say what would happen.  Both steps: `index`
	//     writes the DB, `extract` does not —— and gating on WritesDB is exactly how `extract`
	//     ran against an empty vault and overwrote an eight-hour graph with 117 bytes.
	for _, id := range []string{"index", "extract"} {
		code, body := post(mk(t.TempDir()), id)
		if code != 412 {
			t.Fatalf("%s with an empty vault returned %d, want 412 (body %q)", id, code, body)
		}
		if !strings.Contains(body, "overwrite what is there") {
			t.Errorf("%s: the refusal does not say what would be lost: %q", id, body)
		}
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
	for _, id := range []string{"index", "extract"} {
		if code, body := post(mk(full), id); code == 412 {
			t.Fatalf("%s was refused on a populated vault: %q", id, body)
		}
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

// `verify` walks the repository, and the api image carries only `src/`.
//
//	Run through the web UI in the shipped container it printed
//	"❌ found no .md at all (has a path gone stale?): ['/app']" and the run went to `failed`
//	(run 20260903-164655, measured 2026-09-03).  The guard inside verify_docs.py fired exactly
//	as designed —— but the step could never have worked there, and the UI offered it anyway.
//	Shipping docs/ does not fix it: the relative links inside reach `plugin/` (263MB) and
//	`web/`, which .dockerignore excludes on purpose.
//
//	So it is refused up front, like an LLM step with no credentials.  The condition is evidence
//	—— docs/ next to src/ —— rather than "am I in a container", because running the API from a
//	source checkout is a real configuration that must keep working.
func TestStartRunRefusesRepoStepWithoutRepo(t *testing.T) {
	post := func(s *Server) (int, string) {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, "/api/runs",
			strings.NewReader(`{"step":"verify"}`))
		r.Header.Set("Content-Type", "application/json")
		s.startRun(w, r)
		return w.Code, w.Body.String()
	}
	//  A vault with a real note, so nothing *else* refuses and this test measures its own guard.
	vault := t.TempDir()
	if err := os.WriteFile(filepath.Join(vault, "a.md"),
		[]byte("---\ntitle: a\n---\n"+strings.Repeat("본문 ", 40)), 0o644); err != nil {
		t.Fatal(err)
	}
	mk := func(src string) *Server {
		//  Case ② deliberately passes the guard, which starts a real run —— give it a runs/
		//  directory so the background writer does not fail against a temp dir it does not own.
		home := t.TempDir()
		if err := os.MkdirAll(filepath.Join(home, "runs"), 0o755); err != nil {
			t.Fatal(err)
		}
		return &Server{
			home: home, vault: vault, src: src,
			steps: map[string]Step{"verify": {ID: "verify", NeedsRepo: true}},
			subs:  map[string]map[chan string]struct{}{},
		}
	}

	//  ① src/ with no docs/ beside it —— the container's shape
	bare := filepath.Join(t.TempDir(), "src")
	if err := os.MkdirAll(bare, 0o755); err != nil {
		t.Fatal(err)
	}
	code, body := post(mk(bare))
	if code != 412 {
		t.Fatalf("a repo step with no docs/ returned %d, want 412 (body %q)", code, body)
	}
	//  The message has to name the way out, or the person is stuck at a red run with no next move.
	if !strings.Contains(body, "just verify") {
		t.Errorf("the refusal does not say to run it on the host: %q", body)
	}

	//  ② a real checkout —— the guard must not fire, or it blocks the only place it works
	repo := t.TempDir()
	for _, d := range []string{"src", "docs"} {
		if err := os.MkdirAll(filepath.Join(repo, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if code, body := post(mk(filepath.Join(repo, "src"))); code == 412 {
		t.Fatalf("a source checkout was refused: %q", body)
	}
}

// Every step is classified against the empty-vault guard, and a new one has to be classified too.
//
//	The first version of this pinned the **guarded set** —— it built a map of steps the condition
//	already matched, then checked that map against {extract, index, sync}.  A step that never
//	enters the map cannot change it, so both halves were blind to the case this exists for.
//	Reproduced 2026-09-04 by injecting into status.py:
//
//	    {"id": "summarise", "reads": "your notes **/*.md", "writes": "~/.kal/digest.json", …}
//
//	—— reads the vault, writes a file, completely ungated, `go test` green.  That is the
//	`lr_kg.json` shape one wording away, and the reason `WritesDB` failed in the first place.
//
//	So the iteration is inverted: walk **every** step and demand an entry here.  Adding one to
//	status.py fails this test until somebody decides, which is the property a typed field was
//	supposed to buy —— without a field anyone has to remember to set.  It also keeps a reworded
//	`reads` loud: the derived answer flips against a pinned expectation instead of quietly
//	shrinking a set.
//
//	The table is a second place the step list lives, and that is the price.  It is paid in a test
//	that fails by name the moment it drifts, rather than in a runtime hole nobody sees.
func TestEveryStepIsClassifiedAgainstTheVaultGuard(t *testing.T) {
	py := filepath.Join("..", ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Fatalf("the project venv is missing (%v) —— run `uv sync`", err)
	}
	s := &Server{python: py, src: filepath.Join("..", "src")}
	if err := s.loadSteps(); err != nil {
		t.Fatalf("could not load the real step list: %v", err)
	}

	//  true  = builds from the vault, so an empty one would overwrite something with nothing.
	//  false = does not read the vault at all, or writes nothing.  The reason matters more than
	//          the value; a bare `false` is what lets the next person "fix" it.
	want := map[string]bool{
		"distill": false, //  reads ~/.claude/projects/**, not the vault
		//  ⚠ `promote` **writes** the vault and does not read it, so a guard keyed on "reads the
		//     vault" is the wrong shape for it.  It is the one step that rewrites the user's
		//     notes, and it is guarded in Python where the deletion happens ——
		//     `is_openwiki_bundle` + `vault_is_git` + `would_shrink`.  Do not pull it in here.
		//
		//     ⚠ The asymmetry is a **plan, not an oversight**: every vault-*reading* step is gated
		//        in a Go HTTP handler, while the one vault-*writing* step is gated in Python beside
		//        its writes.  After `lr_kg.json` and the `mode="overwrite"` finding, Python next to
		//        the write is the place we settled on —— so `promote` is the one that is already
		//        right, and the direction of travel is the reader guards following it there, leaving
		//        Go with one advisory pre-flight allowed to be approximate.  Not done here: the
		//        current guard is tested and works, and moving it is a change to make deliberately.
		"promote":       false,
		"extract":       true,  //  vault → ~/.kal/lr_kg.json.  The one that got through.
		"index":         true,  //  vault → every table
		"export":        false, //  reads LanceDB
		"refresh_kg":    false, //  a combo —— it re-runs the steps above, which are each guarded
		"apply_aliases": false, //  combo
		"rebuild_all":   false, //  combo
		"sync":          true,  //  vault → chunks · ix_* · stale_docs
		"verify":        false, //  reads docs/ and the DB, writes nothing
	}
	for id, st := range s.steps {
		exp, known := want[id]
		if !known {
			t.Errorf("step %q is not classified here —— decide whether the empty-vault guard "+
				"covers it and add it to `want` with the reason (reads=%q writes=%q)",
				id, st.Reads, st.Writes)
			continue
		}
		//  ⚠ Ask the production function.  Re-deriving the expression here left mutations of the
		//     handler's copy green (measured 2026-09-04) —— the test agreed with itself.
		if got := buildsFromVault(st); got != exp {
			t.Errorf("step %q: the guard %s it, but this table says %s (reads=%q writes=%q)",
				id, map[bool]string{true: "covers", false: "does not cover"}[got],
				map[bool]string{true: "it should", false: "it should not"}[exp],
				st.Reads, st.Writes)
		}
	}
	//  And the other way —— a name in the table that no longer exists is a stale expectation
	//  that would quietly stop testing anything.
	for id := range want {
		if _, ok := s.steps[id]; !ok {
			t.Errorf("`want` classifies %q, which status.py no longer declares", id)
		}
	}
}

// A guard that cannot answer must say so, not invent a cause.
//
//	One branch used to serve both: a Python crash, a missing `schema_v3`, an import-time
//	`SystemExit` and a genuinely empty vault all reached the user as "there are no notes to read
//	… this container was started without a vault", which the server does not know and which is
//	simply wrong on the host CLI, where there is no container. This repository's own rule, from
//	`status.py`: a screen that claims to know what it does not is the worst failure.
func TestVaultGuardTellsFailureFromEmptiness(t *testing.T) {
	post := func(s *Server) (int, string) {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, "/api/runs", strings.NewReader(`{"step":"index"}`))
		r.Header.Set("Content-Type", "application/json")
		s.startRun(w, r)
		return w.Code, w.Body.String()
	}
	mk := func(python string) *Server {
		return &Server{
			home: t.TempDir(), vault: t.TempDir(), python: python,
			src: filepath.Join("..", "src"),
			steps: map[string]Step{"index": {ID: "index", WritesDB: true,
				Reads: "vault **/*.md", Writes: "documents · chunks"}},
			subs: map[string]map[chan string]struct{}{},
		}
	}

	//  ① the interpreter is not there —— the guard cannot answer
	code, body := post(mk(filepath.Join(t.TempDir(), "no-such-python")))
	if code != 412 {
		t.Fatalf("an unanswerable guard returned %d, want 412 (%q)", code, body)
	}
	if !strings.Contains(body, "could not find out") {
		t.Errorf("a failure was reported as a cause the server does not know: %q", body)
	}
	//  It must not assert the container story, which is wrong on the CLI path.
	if strings.Contains(body, "started without a vault") {
		t.Errorf("a subprocess failure was blamed on a missing vault mount: %q", body)
	}

	//  ①b the interpreter runs but the import blows up —— the traceback has to reach the user.
	//      `.Output()` puts stderr in `ExitError.Stderr`, which `%w` drops, so this arrived as a
	//      bare "exit status 1" with nothing to act on.  A missing interpreter does not cover
	//      this: it fails before Python writes a word.
	py := filepath.Join("..", ".venv", "bin", "python")
	if _, err := os.Stat(py); err != nil {
		t.Fatalf("the project venv is missing (%v) —— run `uv sync`", err)
	}
	broken := mk(py)
	broken.src = t.TempDir() // no schema_v3 here
	code, body = post(broken)
	if code != 412 {
		t.Fatalf("an import failure returned %d, want 412 (%q)", code, body)
	}
	//  ⚠ Assert on something only **stderr** can supply.  A first version also accepted
	//     "schema_v3", which the wrap text itself contains ("could not ask schema_v3 …") —— so it
	//     passed with stderr thrown away, and the mutation stayed green.
	if !strings.Contains(body, "ModuleNotFoundError") {
		t.Errorf("the traceback was dropped —— the user gets nothing to act on: %q", body)
	}

	//  ② a real interpreter and a genuinely empty vault —— the other sentence
	code, body = post(mk(py))
	if code != 412 {
		t.Fatalf("an empty vault returned %d, want 412 (%q)", code, body)
	}
	if !strings.Contains(body, "there are no notes to read") {
		t.Errorf("an empty vault was not reported as one: %q", body)
	}
	//  Both directions, or one message could serve both again without anything noticing.
	if strings.Contains(body, "could not find out") {
		t.Errorf("an empty vault was reported as a failure to answer: %q", body)
	}
}

// `buildsFromVault` itself, including the half no real step distinguishes today.
//
//	The classification test above cannot separate the two clauses: every step whose `reads`
//	names the vault also writes something, so dropping `&& step.Writes != ""` changes nothing
//	and that mutation stayed green (2026-09-04).  The clause is not dead —— it is what keeps a
//	read-only vault step from being refused for a database it would never touch —— so it is
//	pinned here rather than deleted, against inputs the shipped list does not contain yet.
func TestBuildsFromVault(t *testing.T) {
	for _, c := range []struct {
		name          string
		reads, writes string
		want          bool
	}{
		{"reads the vault and writes", "vault **/*.md", "~/.kal/lr_kg.json", true},
		//  The clause the step list cannot exercise: a reader that produces nothing has nothing
		//  to overwrite, so refusing it would be a false alarm on the one path it works.
		{"reads the vault, writes nothing", "vault **/*.md", "", false},
		{"writes but does not read the vault", "~/.kal/distilled/", "vault raw/…", false},
		{"neither", "LanceDB", "", false},
	} {
		if got := buildsFromVault(Step{Reads: c.reads, Writes: c.writes}); got != c.want {
			t.Errorf("%s: got %v, want %v (reads=%q writes=%q)", c.name, got, c.want, c.reads, c.writes)
		}
	}
}

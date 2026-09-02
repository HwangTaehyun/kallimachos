// kallimachos API — runs the pipeline and exposes the knowledge DB's state.
//
// Why Go calls Python
//
//	The pipeline uses LanceDB, sentence-transformers and torch.  Rewriting that in Go would
//	be building the same thing twice, and two versions that diverge answer differently in
//	silence.  So Go does **orchestration only** —— it starts processes, streams their output
//	and stops two from running at once.  The judgements (what is stale, what to run next)
//	live in src/status.py alone and Go passes that JSON straight through.
//
// Where the state lives
//
//	Run records are files (KAL_HOME/runs/<id>.json + .log).  Postgres is not used because all
//	there is to store is "what ran when and what the log said", and files are better for that
//	—— the log can be tailed, a backup is cp, and there are no schema migrations.
//	When several users, permissions and aggregate queries appear, that is when it goes in.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type Step struct {
	ID       string   `json:"id"`
	Title    string   `json:"title"`
	Desc     string   `json:"desc"`
	Group    string   `json:"group"`          // main | combo | partial | check
	Order    int      `json:"order"`          // its position within main
	Runs     []string `json:"runs,omitempty"` // the steps a combo re-runs
	Reads    string   `json:"reads,omitempty"`
	Writes   string   `json:"writes,omitempty"`
	Cmd      []string `json:"cmd"`
	Minutes  int      `json:"minutes"`
	NeedsLLM bool     `json:"needs_llm"`
	WritesDB bool     `json:"writes_db"`
	//  ⚠ Does it delete and rewrite documents inside the user's vault.  The web's `heavy()`
	//    reads this to raise a confirmation.  **Without this field it disappears silently** ——
	//    Python may send it, but absent from this struct Go drops it on re-serialisation.
	//    That is exactly why `promote` ran with no confirmation (deep review 2026-08-28).
	WritesVault bool `json:"writes_vault"`
}

type Group struct {
	ID    string `json:"id"`
	Title string `json:"title"`
	Desc  string `json:"desc"`
}

// claudeReady checks whether `claude -p` actually answers.
//
// Why it is needed —— in a container there may be no authentication.  macOS keeps Claude's
// credentials in **the keychain**, so mounting ~/.claude does not bring them.  Not knowing
// that and starting an extraction burns 30 minutes failing on every chunk.  It asks once, first.
//
// The result is cached —— starting the CLI on every request makes the UI slow.  The window is
// short so that logging in can be re-checked soon after.
type llmProbe struct {
	mu     sync.Mutex
	ok     bool
	msg    string
	when   time.Time
	python string // needed to ask through claude_cli
	src    string
}

func (p *llmProbe) check(force bool) (bool, string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !force && !p.when.IsZero() && time.Since(p.when) < 60*time.Second {
		return p.ok, p.msg
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	// ⚠ claude is not called directly.  Going through claude_cli.run() is what takes the
	//   KAL_CLAUDE_RELAY branch —— called directly it always says "Not logged in" in a
	//   container, so LLM steps are blocked even with a healthy relay running (measured).
	//   Asking through **the same path** the pipeline really uses is what makes the answer right.
	cmd := exec.CommandContext(ctx, p.python, "-c", `
import sys
sys.path.insert(0, `+strconv.Quote(p.src)+`)
from claude_cli import run, RELAY
out = run("haiku", "Reply with exactly: OK", timeout=90)
print(("relay " + RELAY + " · ") if RELAY else "local · ", end="")
print(out.strip() or "(empty response)")
`)
	out, err := cmd.CombinedOutput()
	got := strings.TrimSpace(string(out))
	p.when = time.Now()
	switch {
	case err != nil:
		p.ok, p.msg = false, fmt.Sprintf("claude failed to run: %v (%s)", err, first(got, 120))
	case !strings.Contains(got, "OK"):
		p.ok, p.msg = false, "claude cannot be used: "+first(got, 200)
	default:
		p.ok, p.msg = true, ""
	}
	return p.ok, p.msg
}

// A run id is only ever a timestamp we generated.  Nothing else is accepted.
//
// Why —— it goes straight into filepath.Join(runsDir, id+".json"), and Go 1.22's PathValue
// hands over %2F decoded.  Measured, four stacked `..%2F` read /tmp/proof.json verbatim.
// nginx blocks `..` with a 400 (the production profile), but the development override opens
// the API port directly and other containers on the same compose network can reach the API.
// Blocking it here is right.
// (adversarial review 2026-08-18, security lens)
var runIDRx = regexp.MustCompile(`^[0-9]{8}-[0-9]{6}$`)

// The arguments a caller may pass.  **Only what each step declares** is accepted.
//
// Why —— args are appended to the script's argv verbatim.  No shell is involved so there is no
// shell escape, but any flag argparse accepts goes through.  The real risk was arbitrary file
// writes: {"step":"export","args":["--out","/vault/wiki/index.md"]} overwrites a vault note
// (the vault is mounted rw).
//
// What the UI actually uses is refresh_kg --force and nothing else.  The rest are empty.
// (adversarial review 2026-08-18, security lens)
var allowedArgs = map[string]map[string]bool{
	"refresh_kg": {"--force": true, "--check": true, "--no-export": true},
	"extract":    {"--check-scope": true},
	"export":     {"--no-communities": true},
	"verify":     {"--fix": true},
}

func validRunID(id string) bool { return runIDRx.MatchString(id) }

func first(s string, n int) string {
	s = strings.ReplaceAll(s, "\n", " ")
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

type Run struct {
	ID        string   `json:"id"`
	Step      string   `json:"step"`
	Args      []string `json:"args,omitempty"`
	StartedAt int64    `json:"started_at"`
	EndedAt   int64    `json:"ended_at,omitempty"`
	ExitCode  *int     `json:"exit_code,omitempty"`
	Status    string   `json:"status"` // running | ok | failed | cancelled
	Lines     int      `json:"lines"`
	// Where it was run —— "" (the web) or "cli".  So the screen never leaves someone asking
	// "why is my CLI run not showing".
	Origin string `json:"origin,omitempty"`
}

type Server struct {
	src        string // the absolute path of src/
	python     string // the interpreter
	home       string // KAL_HOME (runs/ is created here)
	vault      string // KAL_VAULT — the galaxy graph lives under it
	vaultName  string // the name that goes into an obsidian:// deep link.  It can differ from the path's basename
	steps      map[string]Step
	stepsOrder []Step
	groups     []Group

	//  The estimate cache —— estimate.py walks the whole vault and takes 3–4 seconds.  Re-running
	//  it on every screen change makes clicking slow.  It is invalidated when a run finishes
	//  (which is when the value really changes).
	estMu   sync.Mutex
	estJSON []byte
	estAt   time.Time

	mu      sync.Mutex
	current *activeRun // one at a time.  The pipeline overwrites the same DB.
	subs    map[string]map[chan string]struct{}
	llm     llmProbe
}

type activeRun struct {
	run    Run
	cancel context.CancelFunc
}

func main() {
	src := envOr("KAL_SRC", "/app/src")
	py := envOr("KAL_PYTHON", "python3")
	home := envOr("KAL_HOME", os.ExpandEnv("$HOME/.kal"))
	addr := envOr("KAL_ADDR", ":8080")
	vault := envOr("KAL_VAULT", "/vault")
	// Inside a container the vault is simply "/vault", so the basename is not the real vault name.
	// Put into obsidian://open?vault=... verbatim, Obsidian cannot find it.
	vaultName := envOr("KAL_VAULT_NAME", filepath.Base(vault))

	s := &Server{src: src, python: py, home: home, vault: vault, vaultName: vaultName,
		subs: map[string]map[chan string]struct{}{}}
	s.llm.python, s.llm.src = py, src
	if err := s.loadSteps(); err != nil {
		log.Fatalf("could not read the pipeline steps (check src/status.py): %v", err)
	}
	if err := os.MkdirAll(s.runsDir(), 0o700); err != nil {
		log.Fatalf("could not create the run-record directory: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", s.health)
	mux.HandleFunc("GET /api/llm", s.llmStatus)
	mux.HandleFunc("GET /api/status", s.status)
	mux.HandleFunc("GET /api/steps", s.listSteps)
	mux.HandleFunc("GET /api/runs", s.listRuns)
	mux.HandleFunc("POST /api/runs", s.startRun)
	mux.HandleFunc("GET /api/runs/{id}", s.getRun)
	mux.HandleFunc("GET /api/runs/{id}/log", s.streamLog)
	mux.HandleFunc("POST /api/runs/{id}/cancel", s.cancelRun)
	mux.HandleFunc("GET /api/graph", s.graph)
	mux.HandleFunc("GET /api/estimate", s.getEstimate)
	mux.HandleFunc("GET /api/config", s.getConfig)
	mux.HandleFunc("PUT /api/config", s.putConfig)
	mux.HandleFunc("GET /api/aliases", s.getAliases)
	mux.HandleFunc("PUT /api/aliases", s.putAliases)
	mux.HandleFunc("GET /api/aliases/suggestions", s.aliasSuggestions)
	mux.HandleFunc("GET /api/homonyms", s.getHomonyms)
	mux.HandleFunc("PUT /api/homonyms", s.putHomonyms)
	mux.HandleFunc("GET /api/homonyms/suggestions", s.homonymSuggestions)

	log.Printf("kallimachos api · src=%s python=%s home=%s · listen %s", src, py, home, addr)
	srv := &http.Server{
		Addr:              addr,
		Handler:           logging(guard(mux)),
		ReadHeaderTimeout: 10 * time.Second,
		// A log stream (SSE) has to stay open for hours, so no write timeout is set.
		WriteTimeout: 0,
	}
	log.Fatal(srv.ListenAndServe())
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func (s *Server) runsDir() string { return filepath.Join(s.home, "runs") }

// ── The pipeline steps are owned by status.py ──────────────────────────────
// Copying the same list into Go guarantees divergence.  It is fetched once at boot.
func (s *Server) loadSteps() error {
	out, err := exec.Command(s.python, "-c", `
import json, sys
sys.path.insert(0, `+strconv.Quote(s.src)+`)
from status import STEPS, GROUPS
print(json.dumps({"steps": STEPS, "groups": GROUPS}, ensure_ascii=False))
`).Output()
	if err != nil {
		return err
	}
	var payload struct {
		Steps  []Step  `json:"steps"`
		Groups []Group `json:"groups"`
	}
	if err := json.Unmarshal(out, &payload); err != nil {
		return err
	}
	s.steps = map[string]Step{}
	for _, st := range payload.Steps {
		s.steps[st.ID] = st
	}
	s.stepsOrder = payload.Steps
	s.groups = payload.Groups
	return nil
}

// A state-changing request must arrive **from the same origin, as JSON**.
//
// Why it is needed —— loopback binding blocks remote hosts but not **the user's own browser**.
// With the UI open, any web page they visit could do this:
//
//	fetch('http://127.0.0.1:5173/api/runs', {method:'POST', mode:'no-cors',
//	      body:'{"step":"promote"}'})
//
// `no-cors` plus a default Content-Type is a CORS **simple request**, so there is no preflight.
// The browser sends it, nginx forwards it and Go runs it.  The response is opaque and cannot be
// read, but **the side effects happen**: `promote` rmtree's the vault's session documents and
// git-commits, and `refresh_kg --force` burns 35 minutes of `claude -p` on the user's account.
// (r4-sec, 2026-08-21)
//
// It is blocked in two layers.
//
//	① Content-Type has to be `application/json` —— this alone closes it.
//	   `no-cors` forces Content-Type into a set of simple values, so json cannot be made, and
//	   making it attaches a preflight that the browser blocks because we send no CORS headers.
//	② If `Origin` is present it has to be ourselves —— belt and braces.
//	   nginx forwards `Host $host` unchanged, so a same-origin request carries the same value.
//
// The web client already always sends `Content-Type: application/json` (web/src/api.ts:89).
// The body cap.  It has to be **the same number** as nginx's `client_max_body_size` (web/nginx.conf).
//
// It used to be `io.LimitReader(r.Body, 1<<20)`.  That **truncates silently** —— send 1.5MB and
// nginx (2m) lets it through, Go cuts it, `json.Decode` reports "unexpected EOF", and the user
// sees `400 could not read the body`.  Nothing anywhere says it was a size problem.
// `MaxBytesReader` reports a real "too large" error.  (r4-fresh, round 5, 2026-08-21)
const maxBody = 2 << 20 // 2MiB — the same as client_max_body_size 2m in web/nginx.conf

// ⚠ **The name comes first** —— `allowedHost` is the first gate.  DNS rebinding cannot be
// blocked by Origin: an attacker's page rebinding `evil.example.com` to 127.0.0.1 makes the
// browser send `Origin: evil.example.com`, which is not "the same origin" but simply someone
// else's name.  Measured (2026-08-23, reproduced by the reviewer): a `POST /api/runs` with
// both Host and Origin set to the attacker's domain **passed with 202**.  It is a 403 today.
//
// The proxy has to use `changeOrigin: false` (`web/vite.config.ts:20`).  With `true`, Host is
// **rewritten** to the target service name `api:8080`, the name the browser sent disappears,
// and this gate has nothing left to check.  `allowedHost` once permitted `api` and `web` to
// accommodate that, which reopens rebinding, so the proxy side was fixed and it was removed
// (9774b84).
//
// Origin remains **the second gate**.  After the name passes, for write requests only:
//
//	evil.example.com  →  not loopback · differs from Host   blocked
//	127.0.0.1:5173    →  loopback                           allowed (our UI)
//	sb.example.com    →  the same as Host                   allowed (the UI of a domain deployment)
//
// The only case this loosens is "a domain deployment where the request comes from a page on the
// victim's **own loopback**", which already means code is running on that machine and is outside
// this defence's scope.  Besides, the docs require **authentication first** for a domain configuration (docs/ARCHITECTURE.md:628).
func selfOrigin(originHost, reqHost string) bool {
	if originHost == "" {
		return false
	}
	return originHost == reqHost || isLoopbackHost(originHost)
}

func isLoopbackHost(hostport string) bool {
	h := hostport
	if x, _, err := net.SplitHostPort(hostport); err == nil {
		h = x
	}
	h = strings.Trim(h, "[]")
	if h == "localhost" {
		return true
	}
	ip := net.ParseIP(h)
	return ip != nil && ip.IsLoopback()
}

// ⚠ **Host is checked first.**  `selfOrigin` lets `originHost == reqHost` through, and a DNS
//
//	rebinding attacker **can make those two equal, on their own domain** ——
//	point `evil.test` back at 127.0.0.1 and the browser sends `Host: evil.test`,
//	`Origin: http://evil.test` and `Sec-Fetch-Site: same-origin`.
//	Measured (deep review 2026-08-25, security lens): that combination made `POST /api/runs`
//	**return 202 with a child process actually started.**
//
//	So before Origin it checks **whether this is a name we agreed to answer to**.  Rebinding
//	cannot cross that —— an attacker cannot make the victim's browser send a Host that is
//	not their own domain.
func allowedHost(hostport, domain string) bool {
	h := hostport
	if x, _, err := net.SplitHostPort(hostport); err == nil {
		h = x
	}
	h = strings.ToLower(strings.Trim(h, "[]"))
	//  A request with no Host (HTTP/1.0, some CLIs) is not a browser —— it passes.
	//  Blocking it breaks curl, and a browser-based attack always carries a Host.
	if h == "" {
		return true
	}
	//  ⚠ `0.0.0.0` is **left out.**  It is a bind address, not a Host a normal client sends.
	//    But some browsers resolve `http://0.0.0.0:port` as loopback, so a public web page can
	//    reach a local service under that name ("0.0.0.0 Day", 2024).  There is no
	//    authentication here, so the name gate is the only defence —— anyone who typed
	//    `0.0.0.0:5173` into the address bar can use `127.0.0.1`/`localhost` instead.
	//    (deep review 2026-08-25)
	if h == "localhost" || h == "127.0.0.1" || h == "::1" {
		return true
	}
	if ip := net.ParseIP(h); ip != nil && ip.IsLoopback() {
		return true
	}
	//  A domain deployment gets only that name (the docs require authentication first for it)
	if d := strings.ToLower(strings.TrimSpace(domain)); d != "" && h == d {
		return true
	}
	return false
}

func guard(h http.Handler) http.Handler {
	domain := os.Getenv("DOMAIN")
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		//  ① The name first.  Rebinding is caught here.
		if !allowedHost(r.Host, domain) {
			fail(w, 403, "this address is not accepted")
			return
		}
		// A GET is not free either.  Four paths start a child process:
		//   /api/llm?fresh=1        **really** calls `claude -p` (fresh bypasses the 60-second
		//                           cache).  Serialised by a mutex, so once every 9 seconds ——
		//                           about 400 an hour, burned on the user's quota.  And startRun
		//                           takes the same lock, so a UI button can stall up to 120 seconds.
		//   /api/status?fresh=1     a full sha256 rescan of the vault
		//   /api/{aliases,homonyms}/suggestions   python + LanceDB, with no mutex and no cache
		//
		// ⚠ **`Origin` cannot be used here.**  A browser does not attach Origin to a subresource
		//   GET such as `<img src=…>` —— widening the Origin check to GET would still let the
		//   `<img>` attack through.  `Sec-Fetch-Site` is attached.
		//     same-origin  our UI                allowed
		//     none         address bar, bookmark allowed (a person opened it)
		//     cross-site / same-site            blocked
		//     (absent)     curl and other non-browsers  allowed —— blocking breaks the CLI
		// (r4-sec, round 5.  Their proposal was Origin, which cannot stop the `<img>` scenario
		//  they themselves raised —— fixed by changing the header.)
		if sfs := r.Header.Get("Sec-Fetch-Site"); sfs != "" &&
			sfs != "same-origin" && sfs != "none" {
			fail(w, 403, "requests from another site are not accepted")
			return
		}
		switch r.Method {
		case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
			if o := r.Header.Get("Origin"); o != "" {
				u, err := url.Parse(o)
				if err != nil || !selfOrigin(u.Host, r.Host) {
					fail(w, 403, "requests from a different origin are not accepted")
					return
				}
			}
			mt, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil || mt != "application/json" {
				fail(w, 415, "Content-Type: application/json is required")
				return
			}
		}
		h.ServeHTTP(w, r)
	})
}

func logging(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t := time.Now()
		h.ServeHTTP(w, r)
		if !strings.HasSuffix(r.URL.Path, "/log") { // SSE prints nothing until it ends
			log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(t).Round(time.Millisecond))
		}
	})
}

// The knowledge graph the galaxy view reads.  The file the pipeline's 'export the graph' made, served as it is.
//
// Where it is read from —— **KAL_HOME first, the vault only as a fallback.**  It used to be read
// only from `<vault>/.obsidian/plugins/kal-galaxy/`, which was this handler's *only* use of the
// vault mount: the api container mounted the whole vault to serve one 6MB file.  Nothing in the
// file justifies that —— it is built from LanceDB and its document references are vault-relative
// (measured 2026-09-02).  Only the Obsidian plugin needs a copy inside a vault, because a plugin
// cannot open a file outside its own.  The vault arm stays so an installation that exported before
// this change keeps working until its next export.
//
// It is about 6MB, so resending it every time is wasteful.  ServeContent handles
// countMarkdown counts .md files under root, stopping at `limit`.  Dot-directories are skipped,
// the way every other walk in this project does.
//
// It answers one question —— **is there anything here to index** —— and it answers it in Go rather
// than by asking Python, because the caller is about to authorise a destructive write and a
// subprocess that itself resolves the vault differently is not evidence about this path.
func countMarkdown(root string, limit int) (int, error) {
	if root == "" {
		return 0, nil
	}
	n := 0
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // an unreadable subtree is not a reason to call the whole vault empty
		}
		if d.IsDir() {
			if name := d.Name(); name != "." && strings.HasPrefix(name, ".") {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(d.Name(), ".md") {
			n++
			if n >= limit {
				return fs.SkipAll
			}
		}
		return nil
	})
	if err != nil {
		return n, err
	}
	return n, nil
}

// If-Modified-Since and a second visit ends in a 304 —— re-run the export and the changed mtime fetches it afresh.
func (s *Server) graph(w http.ResponseWriter, r *http.Request) {
	p := filepath.Join(s.home, "graph_export", "kal-graph.json")
	f, err := os.Open(p)
	if err != nil {
		//  The pre-2026-09-02 location.  Only reached when KAL_HOME has no copy yet.
		p = filepath.Join(s.vault, ".obsidian", "plugins", "kal-galaxy", "kal-graph.json")
		f, err = os.Open(p)
	}
	if err != nil {
		fail(w, http.StatusNotFound,
			"there is no graph yet — run 'export the graph' from the settings screen")
		return
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		fail(w, http.StatusInternalServerError, "the graph file could not be read")
		return
	}
	// The viewer needs this to build obsidian:// links.  It is not inside the graph JSON.
	w.Header().Set("X-Vault-Name", s.vaultName)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	//  ⚠ This response carried no Cache-Control, and browsers then fall back to **heuristic
	//    freshness** (RFC 9111 §4.2.2 —— with neither Expires nor Cache-Control, a cache may
	//    invent a lifetime from Last-Modified).  So re-exporting the graph kept serving the old
	//    6MB JSON with no revalidation —— measured 2026-09-01: the server held 21 English
	//    cluster labels while a fresh window still showed the old Korean ones.
	//    `no-cache` means "revalidate before use", not "do not store".
	//    The 304 design in the comment above is untouched, and a changed mtime is picked up at once.
	w.Header().Set("Cache-Control", "no-cache")
	http.ServeContent(w, r, "kal-graph.json", fi.ModTime(), f)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"ok": true, "steps": len(s.steps)})
}

// Whether LLM steps can run.  The UI uses this to disable a button.
func (s *Server) llmStatus(w http.ResponseWriter, r *http.Request) {
	ok, msg := s.llm.check(r.URL.Query().Get("fresh") == "1")
	writeJSON(w, 200, map[string]any{"ok": ok, "message": msg})
}

func (s *Server) listSteps(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"steps": s.stepsOrder, "groups": s.groups})
}

// status is heavy (it hashes the whole vault), so it is cached briefly.
var (
	statusMu   sync.Mutex
	statusBody []byte
	statusAt   time.Time
)

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	statusMu.Lock()
	defer statusMu.Unlock()
	fresh := r.URL.Query().Get("fresh") == "1"
	if !fresh && statusBody != nil && time.Since(statusAt) < 20*time.Second {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("X-Cache", "hit")
		_, _ = w.Write(statusBody)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.python, filepath.Join(s.src, "status.py"), "--json")
	cmd.Dir = s.src
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		detail := err.Error()
		if errors.As(err, &ee) && len(ee.Stderr) > 0 {
			detail = strings.TrimSpace(string(ee.Stderr))
		}
		fail(w, 500, "the state could not be read: "+detail)
		return
	}
	statusBody, statusAt = out, time.Now()
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("X-Cache", "miss")
	_, _ = w.Write(out)
}

// ── Settings ──────────────────────────────────────────────────────────
//
// The value priority is owned by `src/kal_config.py` (environment > config.json > default).
// Go passes that JSON straight through —— the same idiom as status and steps.  Two copies of
// the rule are guaranteed to diverge.
//
// **Paths (vault, DB) are not changed here.**  They are bind mounts in a container.
// Python supplies `editable:false` along with how to change them, and the screen shows that.
// ── How long will it take ──────────────────────────────────────────────────
//
// STEPS' `minutes` is **a fixed constant**.  Measured across 379 run records (2026-08-24),
// extract ranged from 1 second to 89 minutes —— the declared 30 minutes is neither.  When the
// screen puts that constant into a confirmation dialog as "this takes about 30 minutes", a
// user is scared off a job that really takes 3 seconds.  And the reverse.
//
// The rule is owned by `src/estimate.py` —— Go does not know how to count.
const estTTL = 30 * time.Second

func (s *Server) getEstimate(w http.ResponseWriter, r *http.Request) {
	s.estMu.Lock()
	if s.estJSON != nil && time.Since(s.estAt) < estTTL {
		out := s.estJSON
		s.estMu.Unlock()
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write(out)
		return
	}
	s.estMu.Unlock()

	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.python, filepath.Join(s.src, "estimate.py"), "--json")
	cmd.Dir = s.src
	out, err := cmd.Output()
	if err != nil {
		//  A failed estimate is **not fatal.**  It hands back an empty object and the screen
		//  falls back to the fixed value —— a 500 here would stop the pipeline screen appearing at all.
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write([]byte("{}"))
		return
	}
	s.estMu.Lock()
	s.estJSON, s.estAt = out, time.Now()
	s.estMu.Unlock()
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = w.Write(out)
}

// When a run finishes the estimate goes stale —— that work is done, so less remains.
func (s *Server) invalidateEstimate() {
	s.estMu.Lock()
	s.estJSON, s.estAt = nil, time.Time{}
	s.estMu.Unlock()
}

func (s *Server) getConfig(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.python, filepath.Join(s.src, "kal_config.py"), "--json")
	cmd.Dir = s.src
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		detail := err.Error()
		if errors.As(err, &ee) && len(ee.Stderr) > 0 {
			detail = strings.TrimSpace(string(ee.Stderr))
		}
		fail(w, 500, "the settings could not be read: "+detail)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = w.Write(out)
}

func (s *Server) putConfig(w http.ResponseWriter, r *http.Request) {
	//  The same cap as the other write paths.  Settings are small, but no cap is no cap.
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, 413, "the body is too large")
			return
		}
		fail(w, 400, "the body could not be read")
		return
	}
	//  ⚠ The shape is checked here.  Before handing it to Python it has to be **an object** ——
	//    pass an array or a string and Python dies somewhere ambiguous, and that error string
	//    goes straight to the user.
	var patch map[string]any
	if err := json.Unmarshal(body, &patch); err != nil {
		fail(w, 400, "it has to be a JSON object")
		return
	}
	if len(patch) == 0 {
		fail(w, 400, "there is nothing to change")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.python, filepath.Join(s.src, "kal_config.py"), "--save")
	cmd.Dir = s.src
	cmd.Stdin = bytes.NewReader(body)
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		//  Exit code 2 = validation failed.  The user can fix it, so it is a 400 with the
		//  reason passed through (something like "the overlap must be smaller than the chunk size").
		if errors.As(err, &ee) && ee.ExitCode() == 2 {
			fail(w, 400, strings.TrimSpace(string(ee.Stderr)))
			return
		}
		detail := err.Error()
		if errors.As(err, &ee) && len(ee.Stderr) > 0 {
			detail = strings.TrimSpace(string(ee.Stderr))
		}
		fail(w, 500, "the settings could not be saved: "+detail)
		return
	}
	//  Drop the status cache —— changed settings change the "re-index required" verdict.
	statusBody, statusAt = nil, time.Time{}
	//  The step list is re-read too.  **Setting values go inside the descriptions** ——
	//  the 500 in "chunk the vault into 500 chars" is one.  Read once at boot, a re-index
	//  dialog keeps saying 500 right after the chunk size moved to 900.  In the very place
	//  that also says "this cannot be undone".  (measured 2026-08-22 —— only restarting the
	//  api made the number follow.)
	if err := s.loadSteps(); err != nil {
		//  It does not die.  The settings are already saved and the step list still works, if stale.
		log.Printf("could not re-read the step list after saving settings: %v —— the numbers in the descriptions may be stale", err)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = w.Write(out)
}

// ── Runs ──────────────────────────────────────────────────────────────
func (s *Server) startRun(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Step string   `json:"step"`
		Args []string `json:"args"`
	}
	// Capped like writeYAML(:854).  Under compose nginx blocks first, but
	// `just build-api` produces a standalone binary that runs without nginx.  (r4-fresh)
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBody)).Decode(&body); err != nil {
		//  A size overflow and a format error are **told apart.**  Both used to be "the body
		//  could not be read", and truncated JSON looked like a format error —— nothing said it
		//  was a size problem.  (r4-fresh, round 5)
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, 413, fmt.Sprintf("the body is too large (cap %d MiB)", maxBody>>20))
			return
		}
		fail(w, 400, "the body could not be read")
		return
	}
	step, ok := s.steps[body.Step]
	if !ok {
		fail(w, 400, "unknown step: "+body.Step)
		return
	}
	for _, a := range body.Args {
		if !allowedArgs[step.ID][a] {
			fail(w, 400, fmt.Sprintf("argument not permitted for the '%s' step: %q", step.ID, a))
			return
		}
	}

	// A step that writes the DB first checks **whether something else is already writing**.
	//
	// Why the file is read directly —— flock does not cross the container boundary (measured
	// 2026-08-18: the host can hold it and the container still acquires it, and vice versa).
	// The lock file's **contents** are visible through the bind mount, so the holder line is
	// read instead.  It is not perfect (a race window remains) but it catches the commonest
	// mistake: pressing the web button while `just index` runs on the host.  The proper answer
	// is a single writer, and the relay architecture keeps that (docs/STACK.md §6).
	if step.WritesDB {
		if holder := s.lockHolder(); holder != "" {
			fail(w, 409, "something else is writing the DB — "+holder+
				"  (if it is running on the host, press again once it finishes)")
			return
		}
		//  ⚠ **A step that writes the DB must not run with no notes to read.**  `up-viewer` mounts
		//     no vault, and an empty `KAL_VAULT` then falls through to ~/.kal/config.json —— which
		//     holds a **host** path that does not exist inside the container.  The walk finds
		//     nothing, and "Rebuild knowledge DB" overwrites every table with empty rowsets while
		//     "Incremental sync" classifies all 1,115 documents as deleted.  One click, no
		//     confirmation, and the DB the container exists to serve is gone.
		//     The status screen already says the vault yielded nothing; that warning did not gate
		//     this handler.  (codex review 2026-09-03, blocker #2 —— configuration reproduced)
		if n, err := countMarkdown(s.vault, 1); err != nil || n == 0 {
			fail(w, 412, "there are no notes to read at "+s.vault+
				" — this step rebuilds the knowledge DB from them, so running it now would empty it."+
				"  This container was started without a vault (see just up-viewer);"+
				" run the pipeline on the host instead.")
			return
		}
	}

	// A step that uses the LLM checks authentication **first**.  Otherwise extraction runs for
	// 30 minutes failing on every chunk, and only reading the log to the end reveals it.
	if step.NeedsLLM {
		if ok, msg := s.llm.check(false); !ok {
			fail(w, 412, "this step needs the claude CLI and it is unavailable — "+msg+
				"  (macOS keychain credentials are not mounted into a container.  "+
				"Run it on the host with `just "+step.ID+"`.)")
			return
		}
	}

	s.mu.Lock()
	if s.current != nil {
		cur := s.current.run
		s.mu.Unlock()
		// The pipeline overwrites the same LanceDB with mode="overwrite".  Overlap and a
		// quietly inconsistent DB is left (see src/kal_lock.py).  It is blocked here first.
		fail(w, 409, fmt.Sprintf("'%s' is already running (run %s)", cur.Step, cur.ID))
		return
	}
	id := time.Now().UTC().Format("20060102-150405")
	run := Run{ID: id, Step: step.ID, Args: body.Args,
		StartedAt: time.Now().Unix(), Status: "running"}
	ctx, cancel := context.WithCancel(context.Background())
	s.current = &activeRun{run: run, cancel: cancel}
	s.mu.Unlock()

	go s.exec(ctx, step, run)
	writeJSON(w, 202, run)
}

func (s *Server) exec(ctx context.Context, step Step, run Run) {
	// ⚠ **Every** exit path of this function has to clear s.current.
	//   The two early returns below did not, so one failure to create a log file left the API
	//   permanently at 409 until a restart —— restoring the permissions did not release it
	//   (reproduced).  The defence lives in this one place.
	//   (adversarial review 2026-08-18, correctness lens)
	cleared := false
	defer func() {
		if !cleared {
			s.clearCurrent(run.ID)
		}
	}()

	logPath := filepath.Join(s.runsDir(), run.ID+".log")
	lf, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		log.Printf("log file failed: %v", err)
		s.publish(run.ID, "0\x00\x00done:failed")
		return
	}

	argv := append([]string{}, step.Cmd...)
	argv = append(argv, run.Args...)
	var cmd *exec.Cmd
	if strings.HasSuffix(argv[0], ".sh") {
		cmd = exec.CommandContext(ctx, "bash", append([]string{filepath.Join(s.src, argv[0])}, argv[1:]...)...)
	} else {
		cmd = exec.CommandContext(ctx, s.python, append([]string{filepath.Join(s.src, argv[0])}, argv[1:]...)...)
	}
	cmd.Dir = s.src
	// Without PYTHONUNBUFFERED, progress sits in a buffer for minutes —— that is what most
	// "it looks stuck" reports on a long job come down to.
	// The run id is passed along —— Python carries it as `job` on relay calls, and a cancel
	// kills the relay's children by that id (see cancelRun).
	cmd.Env = append(os.Environ(), "PYTHONUNBUFFERED=1", "KAL_RUN_ID="+run.ID)

	// ⚠ A cancel has to kill **the whole process tree**.
	//
	//   exec.CommandContext signals only the direct child.  rebuild_all.sh is bash with python
	//   underneath —— measured: after a cancel a grandchild survives, and it holds the write end
	//   of the stdout pipe so sc.Scan() never sees EOF.  So cmd.Wait() is never reached and
	//   finish() is never called.  The result: the run stays "running" forever, the API keeps
	//   answering 409, and an orphaned schema_v3.py keeps overwriting for up to an hour the very
	//   LanceDB the mutex existed to protect.
	//
	//   Setpgid creates a new process group and a cancel signals that group (-pid).
	//   WaitDelay is the safeguard against Wait never returning while a pipe is held.
	//   (adversarial review 2026-08-18, correctness lens)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		// Politely first.  Sent to the whole group.
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		return nil
	}
	cmd.WaitDelay = 10 * time.Second

	pipe, err := cmd.StdoutPipe()
	if err != nil {
		log.Printf("pipe failed: %v", err)
		s.finish(run, -1, "failed", fmt.Sprintf("pipe failed: %v", err), lf)
		cleared = true
		return
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		s.finish(run, -1, "failed", fmt.Sprintf("run failed: %v", err), lf)
		cleared = true
		return
	}

	n := 0
	sc := bufio.NewScanner(pipe)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Text()
		n++
		fmt.Fprintln(lf, line)
		// The sequence number rides along so a subscriber can filter lines it already read from
		// the file, and a reconnecting client knows where to resume (see the streamLog comment).
		s.publish(run.ID, fmt.Sprintf("%d\x00%s", n, line))
	}
	err = cmd.Wait()

	code := 0
	status := "ok"
	if ctx.Err() != nil {
		status, code = "cancelled", -1
	} else if err != nil {
		status = "failed"
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			code = ee.ExitCode()
		} else {
			code = -1
		}
	}
	run.Lines = n
	s.finish(run, code, status, "", lf)
	cleared = true
}

func (s *Server) finish(run Run, code int, status, extra string, lf *os.File) {
	//  That work is done, so "what remains" shrank —— the estimate is dropped.
	s.invalidateEstimate()
	if extra != "" {
		fmt.Fprintln(lf, extra)
		s.publish(run.ID, extra)
	}
	_ = lf.Close()
	run.EndedAt = time.Now().Unix()
	run.ExitCode = &code
	run.Status = status
	//  ⚠ This is the run's **only durable record**.  `clearCurrent` just below clears the
	//    in-memory `s.current`, so a failed write here means that run exists **nowhere** ——
	//    a 35-minute job disappears entirely.  It does not die (the log is incidental) but
	//    **it does not pass over it silently** either.
	if b, mErr := json.MarshalIndent(run, "", " "); mErr != nil {
		log.Printf("⚠ failed to serialise the record for run %s: %v —— this run will not be recorded", run.ID, mErr)
	} else if wErr := os.WriteFile(filepath.Join(s.runsDir(), run.ID+".json"), b, 0o600); wErr != nil {
		log.Printf("⚠ failed to write the record for run %s: %v —— this run will not be recorded", run.ID, wErr)
	}

	s.clearCurrent(run.ID)
	s.publish(run.ID, "0\x00\x00done:"+status)

	// Drop the status cache —— the DB may just have changed
	statusMu.Lock()
	statusBody = nil
	statusMu.Unlock()
}

// The holder written in the lock file.  Empty means nobody is writing.
//
// kal_lock.py writes "who · pid · when" when it takes the lock and empties it on release.
// If a child we started wrote it, it is ignored —— that is this API itself.
func (s *Server) lockHolder() string {
	b, err := os.ReadFile(filepath.Join(s.home, ".write.lock"))
	if err != nil {
		//  ⚠ **A lock check has to fail closed.**
		//    The file being **absent** is normal —— it means nobody holds it.
		//    But returning "" when it exists and **cannot be read** (permissions, IO) makes the
		//    caller read "no lock" and let a DB write through —— a second writer attaches while
		//    the host CLI is writing.  That is the very situation this guard exists to prevent
		//    (docs/STACK.md §6, a single writer).
		if os.IsNotExist(err) {
			return ""
		}
		log.Printf("could not read the lock file: %v —— treating it as held, to be safe", err)
		return "the lock file cannot be read (check the permissions)"
	}
	holder := strings.TrimSpace(string(b))
	if holder == "" {
		return ""
	}
	s.mu.Lock()
	running := s.current != nil
	s.mu.Unlock()
	if running {
		return "" // our own run took it —— the single-run check above already blocks that
	}
	return first(holder, 120)
}

// Clear this run if it is still current.  Safe to call several times.
func (s *Server) clearCurrent(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.current != nil && s.current.run.ID == id {
		s.current = nil
	}
}

func (s *Server) cancelRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validRunID(id) {
		fail(w, 400, "not a run-id format")
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.current == nil || s.current.run.ID != id {
		fail(w, 404, "it is not running")
		return
	}
	s.current.cancel()
	// SIGTERM kills only **this container's** process group.  The relay is a separate process
	// running on the host, and its thread is blocked in `p.communicate(timeout=...)` and does
	// not know the client disconnected —— up to 8 (`MAX_INFLIGHT`) `claude -p` children live on
	// for up to 900 more seconds, burning quota and holding the semaphore so the next run
	// starves.  The relay has a `do_DELETE` and **nothing was calling it.**
	// (r4-sec, 2026-08-21)
	go killRelayJob(id)
	writeJSON(w, 202, map[string]string{"status": "cancelling"})
}

// Kill the `claude -p` children this run started on the relay.  A failure passes over quietly
// —— the cancel itself already happened, and this is tidying up what is left.
func killRelayJob(id string) {
	relay := strings.TrimRight(os.Getenv("KAL_CLAUDE_RELAY"), "/")
	if relay == "" {
		return // a local run —— there is no relay
	}
	req, err := http.NewRequest(http.MethodDelete, relay+"/job/"+url.PathEscape(id), nil)
	if err != nil {
		return
	}
	if t := os.Getenv("KAL_RELAY_TOKEN"); t != "" {
		req.Header.Set("X-Relay-Token", t)
	}
	c := &http.Client{Timeout: 5 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		log.Printf("relay cancel failed %s: %v", id, err)
		return
	}
	defer resp.Body.Close()
	log.Printf("relay cancel %s → %s", id, resp.Status)
}

func (s *Server) listRuns(w http.ResponseWriter, r *http.Request) {
	//  ⚠ **"there is no record" and "it cannot be read" are different.**  Swallowing it turns a
	//    permission or IO error into an empty list and the screen says "no run records" ——
	//    indistinguishable from a fresh install.  The directory **not existing yet** is normal
	//    (before the first run).  The same thing was fixed for stale_docs in status.py.
	ents, err := os.ReadDir(s.runsDir())
	if err != nil && !os.IsNotExist(err) {
		log.Printf("could not read the run-record directory: %v", err)
		fail(w, 500, "the run records could not be read —— check the permissions on "+s.runsDir())
		return
	}
	var out []Run
	skipped := 0
	for _, e := range ents {
		if !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(s.runsDir(), e.Name()))
		if err != nil {
			skipped++
			continue
		}
		var run Run
		if json.Unmarshal(b, &run) == nil {
			out = append(out, run)
		} else {
			skipped++
		}
	}
	//  One broken file does not kill the whole list —— but **it does not disappear silently**
	//  either, so one line is logged.  A run vanishing from the screen with no reason cannot be traced.
	if skipped > 0 {
		log.Printf("skipped %d run record(s) (unreadable, or corrupt JSON)", skipped)
	}
	s.mu.Lock()
	if s.current != nil {
		out = append(out, s.current.run)
	}
	s.mu.Unlock()
	sort.Slice(out, func(i, j int) bool { return out[i].StartedAt > out[j].StartedAt })
	if len(out) > 50 {
		out = out[:50]
	}
	if out == nil {
		out = []Run{}
	}
	writeJSON(w, 200, out)
}

func (s *Server) getRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validRunID(id) {
		fail(w, 400, "not a run-id format")
		return
	}
	s.mu.Lock()
	if s.current != nil && s.current.run.ID == id {
		run := s.current.run
		s.mu.Unlock()
		writeJSON(w, 200, run)
		return
	}
	s.mu.Unlock()
	b, err := os.ReadFile(filepath.Join(s.runsDir(), id+".json"))
	if err != nil {
		fail(w, 404, "no such run")
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = w.Write(b)
}

// ── The log stream (SSE) ──────────────────────────────────────────────
// What has already finished is replayed from the file first, and the live stream continues
// from there.  So a refresh shows it from the beginning again.
func (s *Server) streamLog(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validRunID(id) {
		fail(w, 400, "not a run-id format")
		return
	}
	fl, ok := w.(http.Flusher)
	if !ok {
		fail(w, 500, "this response does not support streaming")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no") // so nginx does not buffer the SSE
	w.WriteHeader(200)

	// ⚠ Subscribing happens **before the file replay**.  The other order loses any line
	//   emitted in between.  It does create duplicates, since the same line arrives from both
	//   the file and the channel —— measured, 50 of 20,000 lines came twice —— and the seq
	//   comparison below filters those.  A loss cannot be undone; a duplicate can be filtered,
	//   so this is the right direction.
	ch := s.subscribe(id)
	defer s.unsubscribe(id, ch)

	// Reconnection support —— EventSource reattaches automatically when the connection drops
	// and sends the last id it received as Last-Event-ID.  Ignoring it floods tens of thousands
	// of log lines back in, and the UI's cap (4,000 lines) cuts off the live tail.
	resume := 0
	if v := r.Header.Get("Last-Event-ID"); v != "" {
		if k, err := strconv.Atoi(v); err == nil && k > 0 {
			resume = k
		}
	}

	replayed := 0
	if f, err := os.Open(filepath.Join(s.runsDir(), id+".log")); err == nil {
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for sc.Scan() {
			replayed++
			if replayed <= resume {
				continue
			}
			fmt.Fprintf(w, "id: %d\ndata: %s\n\n", replayed, sc.Text())
		}
		_ = f.Close()
		fl.Flush()
	}
	if resume > replayed {
		replayed = resume
	}

	s.mu.Lock()
	live := s.current != nil && s.current.run.ID == id
	s.mu.Unlock()
	if !live {
		fmt.Fprint(w, "event: done\ndata: finished\n\n")
		fl.Flush()
		return
	}

	ping := time.NewTicker(20 * time.Second)
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ping.C:
			fmt.Fprint(w, ": ping\n\n") // so a proxy does not drop an idle connection
			fl.Flush()
		case msg, ok := <-ch:
			if !ok {
				return
			}
			seqStr, line, found := strings.Cut(msg, "\x00")
			if !found {
				continue // ignore anything not in the format (impossible, but defensive)
			}
			if strings.HasPrefix(line, "\x00done:") {
				fmt.Fprintf(w, "event: done\ndata: %s\n\n", strings.TrimPrefix(line, "\x00done:"))
				fl.Flush()
				return
			}
			seq, err := strconv.Atoi(seqStr)
			if err != nil || seq <= replayed {
				continue // already sent during the file replay
			}
			replayed = seq
			fmt.Fprintf(w, "id: %d\ndata: %s\n\n", seq, line)
			fl.Flush()
		}
	}
}

func (s *Server) subscribe(id string) chan string {
	ch := make(chan string, 256)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.subs[id] == nil {
		s.subs[id] = map[chan string]struct{}{}
	}
	s.subs[id][ch] = struct{}{}
	return ch
}

func (s *Server) unsubscribe(id string, ch chan string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if m := s.subs[id]; m != nil {
		delete(m, ch)
		if len(m) == 0 {
			delete(s.subs, id)
		}
	}
}

func (s *Server) publish(id, line string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for ch := range s.subs[id] {
		select {
		case ch <- line:
		default: // one slow subscriber does not stall the pipeline
		}
	}
}

// ── Aliases ───────────────────────────────────────────────────────────
func (s *Server) aliasPath() string {
	return envOr("KAL_ALIASES", filepath.Join(filepath.Dir(s.src), "aliases.yml"))
}

// Homonym rules —— the opposite direction to aliases (one name → several nodes).
// Save validation goes through the same code: both files are YAML a person hand-writes, and
// saved broken, the next build dies **somewhere far from here**.
func (s *Server) homonymPath() string {
	return envOr("KAL_HOMONYMS", filepath.Join(filepath.Dir(s.src), "homonyms.yml"))
}

func (s *Server) getHomonyms(w http.ResponseWriter, r *http.Request) {
	s.readYAML(w, s.homonymPath())
}

func (s *Server) putHomonyms(w http.ResponseWriter, r *http.Request) {
	s.writeYAML(w, r, s.homonymPath())
}

func (s *Server) homonymSuggestions(w http.ResponseWriter, r *http.Request) {
	args := []string{filepath.Join(s.src, "homonym_suggest.py"), "--yaml"}
	if r.URL.Query().Get("weak") == "1" {
		args = append(args, "--weak")
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.python, args...)
	cmd.Dir = s.src
	out, err := cmd.Output()
	if err != nil {
		fail(w, 500, "the candidates could not be produced: "+err.Error())
		return
	}
	// The alias side emits JSON while this one's artifact is **YAML to paste** ——
	// a person has to fill in the cues, so structuring it would only end up in an editor anyway.
	writeJSON(w, 200, map[string]string{"yaml": string(out)})
}

func (s *Server) getAliases(w http.ResponseWriter, r *http.Request) {
	s.readYAML(w, s.aliasPath())
}

func (s *Server) readYAML(w http.ResponseWriter, path string) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			writeJSON(w, 200, map[string]string{"content": ""})
			return
		}
		fail(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]string{"content": string(b)})
}

func (s *Server) putAliases(w http.ResponseWriter, r *http.Request) {
	s.writeYAML(w, r, s.aliasPath())
}

func (s *Server) writeYAML(w http.ResponseWriter, r *http.Request, path string) {
	var body struct {
		Content string `json:"content"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBody)).Decode(&body); err != nil {
		//  A size overflow and a format error are **told apart.**  Both used to be "the body
		//  could not be read", and truncated JSON looked like a format error —— nothing said it
		//  was a size problem.  (r4-fresh, round 5)
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, 413, fmt.Sprintf("the body is too large (cap %d MiB)", maxBody>>20))
			return
		}
		fail(w, 400, "the body could not be read")
		return
	}
	// It is confirmed to parse before being written.  Saving broken YAML kills the next build,
	// and that failure appears far from here, making the cause hard to find.
	cmd := exec.Command(s.python, "-c", `
import sys, yaml

src = sys.stdin.read()

# ⚠ safe_load blocks code execution but **still expands anchors and merge keys.**  A
#   billion-laughs fits inside 1MB, and then it is not this validation process that dies but
#   entity_resolve.load_aliases() on every later run.  It is blocked **before** expansion.
#
#   Counting characters does not work —— 3 anchors and 27 aliases already reach 9^4 = 6,561
#   (measured), and a legitimate name like 'R&D' produces a false positive.  Parser events are
#   inspected instead: yaml.parse() streams without expanding, so the bomb never goes off.
#   (adversarial review 2026-08-18, security lens)
# A duplicated top-level key makes safe_load keep **only the last one** —— a variant entered
# earlier disappears silently while the save succeeds.  It is caught in the same parser pass.
depth, seen_keys, expect_key = 0, set(), False
for ev in yaml.parse(src):
    if isinstance(ev, yaml.AliasEvent):
        raise SystemExit("aliases (*), anchors (&) and merge keys (<<) cannot be used — "
                         "this file does not need them, and they are the route for an expansion bomb")
    if getattr(ev, "anchor", None):
        raise SystemExit("anchors (&) cannot be used")
    if isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
        depth += 1
        expect_key = depth == 1 and isinstance(ev, yaml.MappingStartEvent)
        continue
    if isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
        depth -= 1
        expect_key = depth == 1
        continue
    if depth == 1 and isinstance(ev, yaml.ScalarEvent):
        if expect_key:
            if ev.value in seen_keys:
                raise SystemExit(f"{ev.value!r} appears twice — "
                                 "YAML keeps only the last, so the earlier variants disappear")
            seen_keys.add(ev.value)
        expect_key = not expect_key

d = yaml.safe_load(src) or {}
if not isinstance(d, dict):
    raise SystemExit("the top level is not a mapping (the form is representative: [variants...])")
if len(d) > 2000:
    raise SystemExit("too many entries (over 2000)")
for k, v in d.items():
    if v is not None and not isinstance(v, list):
        raise SystemExit(f"the value of {k!r} is not a list")
    if v is not None and len(v) > 200:
        raise SystemExit(f"{k!r} has too many variants (over 200)")
    for x in (v or []):
        if not isinstance(x, (str, int, float)):
            raise SystemExit(f"the variants of {k!r} have to be strings")
`)
	cmd.Stdin = strings.NewReader(body.Content)
	if out, err := cmd.CombinedOutput(); err != nil {
		fail(w, 400, "the YAML is not valid: "+strings.TrimSpace(string(out)))
		return
	}
	if err := os.WriteFile(path, []byte(body.Content), 0o644); err != nil {
		fail(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{
		"saved": true,
		"note":  "to reflect it in the graph, run 'refresh the stale KG' with --force.",
	})
}

func (s *Server) aliasSuggestions(w http.ResponseWriter, r *http.Request) {
	args := []string{filepath.Join(s.src, "alias_suggest.py"), "--json"}
	if r.URL.Query().Get("weak") == "1" {
		args = append(args, "--weak")
	}
	if md := r.URL.Query().Get("min_docs"); md != "" {
		args = append(args, "--min-docs", md)
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.python, args...)
	cmd.Dir = s.src
	out, err := cmd.Output()
	if err != nil {
		fail(w, 500, "the candidates could not be produced: "+err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = w.Write(out)
}

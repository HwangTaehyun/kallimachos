package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Why this file exists
//
//   On 2026-08-22 the CSRF guard was blocking **every write** from the web UI with a 403.
//   The run button, saving aliases, all of it.  And yet `go vet` and `go build` were clean,
//   and the web tests mock the api so they saw nothing.
//
//   Why it went unseen is the point: the curl verification sent an `Origin` matching `Host` ——
//   **it never once went through the path the app actually takes (the proxy).**  It confirmed
//   what the guard blocks and never checked that what should pass does.
//
//   So this checks **both directions together**.  With only blocking tests, "block everything"
//   scores full marks.

func TestSelfOrigin(t *testing.T) {
	for _, c := range []struct {
		name, origin, host string
		want               bool
	}{
		//  What has to pass ——————————————————————————————
		{"the same origin", "127.0.0.1:8080", "127.0.0.1:8080", true},
		{"the vite proxy (:5173 → :8080)", "127.0.0.1:5173", "127.0.0.1:8080", true},
		{"the nginx proxy (:80 → :8080)", "127.0.0.1", "127.0.0.1:8080", true},
		//  ⚠ Two rows here used `api:8080` as the Host.  They assumed vite ran with
		//    `changeOrigin: true`, and it is `false` now (`web/vite.config.ts:20`).
		//    Those rows called `selfOrigin` alone and so never passed through
		//    `allowedHost` —— which really does block `api`.  They documented a
		//    configuration that no longer exists, so they were deleted, and
		//    `TestAllowedHost` below calls that function directly instead.
		{"localhost mixed with 127.0.0.1", "localhost:5173", "127.0.0.1:8080", true},
		{"IPv6 loopback", "[::1]:5173", "127.0.0.1:8080", true},
		{"an exact domain match (the proxy profile)", "sb.example.com", "sb.example.com", true},

		//  What has to be blocked ——————————————————————————————
		{"a public site", "evil.example.com", "127.0.0.1:8080", false},
		{"a name that looks like loopback", "127.0.0.1.evil.com", "127.0.0.1:8080", false},
		{"another machine on the LAN", "192.168.0.9:8080", "127.0.0.1:8080", false},
		{"a different domain on a domain deployment", "evil.com", "sb.example.com", false},
		//  ⚠ This one row passes **deliberately**.  The proxy rewrites Host, so "loopback on
		//    both sides" cannot be required and the judgement was put on the Origin side.  What
		//    it loosens is only "a domain deployment plus a page on the victim's own loopback",
		//    authentication first for a domain configuration.
		{"a loopback origin on a domain deployment (deliberately allowed)", "127.0.0.1:5173", "sb.example.com", true},
		{"an empty host", "", "127.0.0.1:8080", false},
	} {
		if got := selfOrigin(c.origin, c.host); got != c.want {
			t.Errorf("%s: selfOrigin(%q, %q) = %v, want %v", c.name, c.origin, c.host, got, c.want)
		}
	}
}

// Does it pass through the whole guard —— a correct helper with wrong wiring is useless.
func TestGuardWrites(t *testing.T) {
	ok := guard(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(200)
	}))

	do := func(method, origin, ct, sfs string) int {
		r := httptest.NewRequest(method, "http://127.0.0.1:8080/api/config", strings.NewReader("{}"))
		r.Host = "127.0.0.1:8080"
		if origin != "" {
			r.Header.Set("Origin", origin)
		}
		if ct != "" {
			r.Header.Set("Content-Type", ct)
		}
		if sfs != "" {
			r.Header.Set("Sec-Fetch-Site", sfs)
		}
		w := httptest.NewRecorder()
		ok.ServeHTTP(w, r)
		return w.Code
	}

	//  ★ This is what was broken —— the shape the web UI actually sends
	if c := do("PUT", "http://127.0.0.1:5173", "application/json", "same-origin"); c != 200 {
		t.Errorf("a normal write from the web UI is blocked with %d (behind the proxy, :5173 → :8080)", c)
	}
	if c := do("POST", "http://localhost:5173", "application/json; charset=utf-8", "same-origin"); c != 200 {
		t.Errorf("a localhost origin with a charset on the Content-Type gives %d", c)
	}
	//  The CLI (no Origin, no Sec-Fetch-Site) has to keep working
	if c := do("POST", "", "application/json", ""); c != 200 {
		t.Errorf("curl and friends are blocked with %d", c)
	}

	//  What has to be blocked
	if c := do("PUT", "https://evil.example.com", "application/json", "cross-site"); c != 403 {
		t.Errorf("cross-site gives %d (should be 403)", c)
	}
	if c := do("PUT", "https://evil.example.com", "application/json", ""); c != 403 {
		t.Errorf("cross-origin with no Sec-Fetch-Site gives %d (should be 403)", c)
	}
	if c := do("PUT", "http://127.0.0.1:5173", "text/plain", "same-origin"); c != 415 {
		t.Errorf("a form-style Content-Type gives %d (should be 415)", c)
	}
	//  A GET does not consult Origin, so Sec-Fetch-Site alone decides
	if c := do("GET", "", "", "cross-site"); c != 403 {
		t.Errorf("a cross-site GET gives %d (should be 403)", c)
	}
	if c := do("GET", "", "", "none"); c != 200 {
		t.Errorf("a GET opened from the address bar gives %d", c)
	}
}

// `allowedHost` is called **directly**.  It is the only gate against DNS rebinding and had no
// test at all (it is the function 9774b84 fixed).  `TestGuardWrites` checks the wiring; this
// checks the judgement.
func TestAllowedHost(t *testing.T) {
	for _, c := range []struct {
		name, host, domain string
		want               bool
	}{
		//  Passes ——————————————————————————————
		{"a loopback name", "localhost:5173", "", true},
		{"a loopback IP", "127.0.0.1:8080", "", true},
		{"loopback IPv6", "[::1]:8080", "", true},
		//  ★ `0.0.0.0` is **blocked** —— some browsers resolve it as loopback, letting a
		//    public page reach a local service ("0.0.0.0 Day").
		{"0.0.0.0 (a bind address)", "0.0.0.0:8080", "", false},
		{"0.0.0.0 with no port", "0.0.0.0", "", false},
		{"another address in 127/8", "127.0.0.2:8080", "", true},
		{"no port", "localhost", "", true},
		{"no Host (curl, HTTP/1.0)", "", "", true},
		{"that domain on a domain deployment", "sb.example.com", "sb.example.com", true},
		{"domain case differences", "SB.Example.COM", "sb.example.com", true},

		//  Blocked ——————————————————————————————
		//  ★ Rebinding itself.  An attacker rebinding their own domain to 127.0.0.1 reaches
		//    loopback while the Host is still the attacker's name.  This once passed with a
		//    202 (reproduced by the reviewer, 2026-08-23).
		{"rebinding —— the attacker's domain", "evil.example.com", "", false},
		{"rebinding —— a name that looks like loopback", "127.0.0.1.evil.com", "", false},
		//  ★ A container service name.  With `changeOrigin: false` the name the browser sent
		//    arrives unchanged, so this value should never appear, and must be blocked if it does.
		{"the service name api", "api:8080", "", false},
		{"the service name web", "web:5173", "", false},
		{"another machine on the LAN", "192.168.0.9:8080", "", false},
		{"a different domain on a domain deployment", "evil.com", "sb.example.com", false},
		{"a domain name with no domain configured", "sb.example.com", "", false},
	} {
		if got := allowedHost(c.host, c.domain); got != c.want {
			t.Errorf("%s: allowedHost(%q, %q) = %v, want %v", c.name, c.host, c.domain, got, c.want)
		}
	}
}

// TestNamedTestsExist —— a comment that names a test as its guarantor must name one that exists.
//
//	Comments in this package cite tests by name ("`Test<Name>` pins this" —— written with angle
//	brackets right here so this very sentence is not read as a citation), and those names are how a
//	later reader decides a clause is safe to trust.  Three of them named tests that had been
//	renamed away, so the sentence "it has a named guarantor rather than being trusted" was itself
//	untrue —— found by an adversarial review on 2026-09-04, after a rename left the prose behind.
//	Renaming a test now breaks this instead of quietly turning a comment into fiction.
func TestNamedTestsExist(t *testing.T) {
	have := map[string]bool{}
	srcs, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	var all []byte
	for _, f := range srcs {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		all = append(all, b...)
	}
	for _, m := range regexp.MustCompile(`(?m)^func (Test[A-Za-z0-9_]+)\(`).FindAllSubmatch(all, -1) {
		have[string(m[1])] = true
	}
	if len(have) == 0 {
		t.Fatal("no test functions found at all —— this check would pass vacuously")
	}
	for _, m := range regexp.MustCompile("`(Test[A-Za-z0-9_]+)`").FindAllSubmatch(all, -1) {
		if name := string(m[1]); !have[name] {
			t.Errorf("a comment names `%s` as its guarantor, but no such test exists", name)
		}
	}
}

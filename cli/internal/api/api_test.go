package api

import (
	"bytes"
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// rendered runs ui.Report on err with the streams captured and returns what
// reached stderr - the text the operator would read.
func rendered(t *testing.T, err error) string {
	t.Helper()
	if err == nil {
		t.Fatal("expected a refusal, got nil")
	}
	var f *ui.Failure
	if !errors.As(err, &f) {
		t.Fatalf("refusal is a %T, want *ui.Failure: %v", err, err)
	}
	stderr := &bytes.Buffer{}
	savedErr, savedOut := ui.Stderr, ui.Stdout
	ui.Stderr, ui.Stdout = stderr, &bytes.Buffer{}
	t.Setenv("NO_COLOR", "1")
	defer func() { ui.Stderr, ui.Stdout = savedErr, savedOut }()
	if code := ui.Report(err); code == 0 {
		t.Fatal("a refusal reported exit 0")
	}
	return stderr.String()
}

// tdd/unit/scripts/test_cli_errors.py::TestBackendUrl
func TestResolveServerURL(t *testing.T) {
	t.Run("a schemeless url is refused not guessed", func(t *testing.T) {
		t.Setenv(ServerEnvVar, "localhost:8790")
		_, err := ResolveServerURL("")
		text := rendered(t, err)
		if !strings.Contains(text, "scheme") {
			t.Fatalf("refusal does not name the scheme:\n%s", text)
		}
		if !strings.Contains(text, "http://localhost:8790") {
			t.Fatalf("the refusal must show the fix:\n%s", text)
		}
	})
	t.Run("the schemeless refusal says why guessing is wrong", func(t *testing.T) {
		t.Setenv(ServerEnvVar, "localhost:8790")
		_, err := ResolveServerURL("")
		if text := rendered(t, err); !strings.Contains(text, "clear") {
			t.Fatalf("guessing http:// downgrades an https URL; the refusal must say so:\n%s", text)
		}
	})
	t.Run("an unknown scheme is refused and the guess strips it", func(t *testing.T) {
		_, err := ResolveServerURL("ftp://h:1")
		if text := rendered(t, err); !strings.Contains(text, "--server http://h:1") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("an empty server setting is refused", func(t *testing.T) {
		t.Setenv(ServerEnvVar, "")
		_, err := ResolveServerURL("")
		if text := rendered(t, err); !strings.Contains(text, "empty") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("an explicit empty --server is refused not defaulted", func(t *testing.T) {
		// cli.py:185 `server if server is not None`: `--server ""` is a
		// refusal naming the flag, never a fall-through to a valid
		// $LAZYAF_SERVER sitting in the shell (verifier finding V4-2).
		t.Setenv(ServerEnvVar, "http://env:1")
		_, err := ResolveServer(ServerSetting{Value: "", Explicit: true})
		text := rendered(t, err)
		if !strings.Contains(text, "--server is empty") {
			t.Fatalf("refusal must name the flag as the empty source:\n%s", text)
		}
		if strings.Contains(text, "http://env:1") {
			t.Fatalf("an explicit empty flag must not fall through to the environment:\n%s", text)
		}
		if _, err := NewFromSetting(ServerSetting{Value: "", Explicit: true}); err == nil {
			t.Fatal("NewFromSetting accepted an explicit empty --server")
		}
		if got := DescribeSetting(ServerSetting{Value: "", Explicit: true}); !strings.Contains(got, "from --server") {
			t.Fatalf("DescribeSetting must attribute the empty URL to --server: %q", got)
		}
		// The bare-string entry point cannot see the bit - stated, and
		// pinned so nobody "fixes" it by guessing.
		if got, err := ResolveServerURL(""); err != nil || got != "http://env:1" {
			t.Fatalf("ResolveServerURL(\"\") = %q, %v; a plain empty value means unset", got, err)
		}
	})
	t.Run("a trailing slash does not become a double slash 404", func(t *testing.T) {
		t.Setenv(ServerEnvVar, "http://h:8790/")
		got, err := ResolveServerURL("")
		if err != nil || got != "http://h:8790" {
			t.Fatalf("ResolveServerURL = %q, %v", got, err)
		}
		got, err = ResolveServerURL("https://h:8790///")
		if err != nil || got != "https://h:8790" {
			t.Fatalf("ResolveServerURL(explicit) = %q, %v", got, err)
		}
	})
	t.Run("the flag wins over the environment", func(t *testing.T) {
		t.Setenv(ServerEnvVar, "http://env:1")
		got, err := ResolveServerURL("http://flag:2")
		if err != nil || got != "http://flag:2" {
			t.Fatalf("ResolveServerURL = %q, %v", got, err)
		}
	})
	t.Run("the message says where the url came from", func(t *testing.T) {
		for _, tc := range []struct{ env, explicit, want string }{
			{"", "http://x", "--server"},
			{"http://y", "", "$LAZYAF_SERVER"},
			{"", "", "the built-in default"},
		} {
			if tc.env != "" {
				t.Setenv(ServerEnvVar, tc.env)
			} else {
				t.Setenv(ServerEnvVar, "")
				// Setenv("") leaves the var present; the default case needs it absent.
				unsetenv(t, ServerEnvVar)
			}
			if got := DescribeServer(tc.explicit); !strings.Contains(got, tc.want) {
				t.Fatalf("DescribeServer(%q) with env %q = %q, want %q", tc.explicit, tc.env, got, tc.want)
			}
		}
	})
	t.Run("describe server names both ways to change it", func(t *testing.T) {
		unsetenv(t, ServerEnvVar)
		described := DescribeServer("")
		if !strings.Contains(described, "--server") || !strings.Contains(described, "LAZYAF_SERVER") {
			t.Fatalf("DescribeServer = %q", described)
		}
		if !strings.Contains(described, DefaultServer) {
			t.Fatalf("DescribeServer must name the URL in use: %q", described)
		}
	})
}

func unsetenv(t *testing.T, key string) {
	t.Helper()
	t.Setenv(key, "") // registers the restore
	if err := os.Unsetenv(key); err != nil {
		t.Fatalf("unsetenv %s: %v", key, err)
	}
}

// server starts an httptest server whose one handler is h and returns a
// client pointed at it with a short timeout.
func server(t *testing.T, h http.HandlerFunc) (*Client, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	c, err := New(srv.URL)
	if err != nil {
		t.Fatalf("New(%s): %v", srv.URL, err)
	}
	c.HTTP = &http.Client{Timeout: 5 * time.Second}
	return c, srv
}

func reply(status int, contentType, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if contentType != "" {
			w.Header().Set("Content-Type", contentType)
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
}

// tdd/unit/scripts/test_cli_errors.py::TestTheServerIsQuoted
func TestServerWordsQuoted(t *testing.T) {
	ctx := context.Background()
	t.Run("a string detail is printed", func(t *testing.T) {
		c, _ := server(t, reply(400, "application/json", `{"detail": "Repo 'x' has no commits yet"}`))
		text := rendered(t, c.Get(ctx, "/api/repos", nil))
		if !strings.Contains(text, "Repo 'x' has no commits yet") {
			t.Fatalf("the server's words were dropped:\n%s", text)
		}
		if !strings.Contains(text, "HTTP 400 for GET /api/repos") {
			t.Fatalf("the summary must name status, method and path:\n%s", text)
		}
	})
	t.Run("a pydantic 422 envelope is rendered readably", func(t *testing.T) {
		c, _ := server(t, reply(422, "application/json",
			`{"detail": [{"type": "string_too_short", "loc": ["body", "name"], "msg": "String should have at least 1 character"}]}`))
		text := rendered(t, c.Post(ctx, "/api/repos/ingest", map[string]string{"name": ""}, nil))
		if !strings.Contains(text, "name: String should have at least 1 character") {
			t.Fatalf("envelope not rendered:\n%s", text)
		}
		if strings.Contains(text, "string_too_short") {
			t.Fatalf("the envelope leaked through:\n%s", text)
		}
	})
	t.Run("a non json error body is still shown", func(t *testing.T) {
		c, _ := server(t, reply(502, "text/html", "<h1>Bad Gateway</h1>"))
		if text := rendered(t, c.Get(ctx, "/api/repos", nil)); !strings.Contains(text, "Bad Gateway") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("a status with no body says so rather than going quiet", func(t *testing.T) {
		c, _ := server(t, reply(500, "", ""))
		text := rendered(t, c.Get(ctx, "/api/repos", nil))
		if !strings.Contains(text, "500") || !strings.Contains(text, "no explanation") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("a 404 uses the commands own words when it has them", func(t *testing.T) {
		c, _ := server(t, reply(404, "application/json", `{"detail": "Repo not found"}`))
		text := rendered(t, c.Get(ctx, "/api/repos/abc/branches", nil,
			NotFound("repo abc does not exist. `lazyaf list` shows the ids that do.")))
		if !strings.Contains(text, "`lazyaf list` shows the ids") {
			t.Fatalf("refusal:\n%s", text)
		}
		if !strings.Contains(text, "Repo not found") {
			t.Fatalf("the server's own line is still quoted:\n%s", text)
		}
	})
	t.Run("a 200 that is not json blames the right component", func(t *testing.T) {
		// Pointing at a dev server or a proxy error page is a setup mistake,
		// not "invalid JSON".
		c, _ := server(t, reply(200, "text/html", "<!doctype html>"))
		if text := rendered(t, c.Get(ctx, "/api/repos", nil)); !strings.Contains(text, "not the LazyAF API") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("a valid answer decodes into the typed struct", func(t *testing.T) {
		c, _ := server(t, reply(200, "application/json",
			`[{"id": "abc", "name": "demo", "is_ingested": true, "remote_url": null, "default_branch": "main", "internal_git_url": "http://h/git/abc", "created_at": "2026-09-16T00:00:00Z"}]`))
		var repos []Repo
		if err := c.Get(ctx, "/api/repos", &repos); err != nil {
			t.Fatalf("Get: %v", err)
		}
		if len(repos) != 1 || repos[0].ID != "abc" || repos[0].Name != "demo" || !repos[0].IsIngested || repos[0].RemoteURL != nil {
			t.Fatalf("decoded %+v", repos)
		}
	})
	t.Run("the request body goes out as json and the path is joined once", func(t *testing.T) {
		var gotPath, gotBody, gotType string
		c, _ := server(t, func(w http.ResponseWriter, r *http.Request) {
			gotPath = r.URL.Path
			gotType = r.Header.Get("Content-Type")
			b := make([]byte, 1024)
			n, _ := r.Body.Read(b)
			gotBody = string(b[:n])
			reply(200, "application/json", `{"created": 1, "updated": 2, "orphaned": 3}`)(w, r)
		})
		var out ReconcileResponse
		err := c.Post(ctx, "/api/test-refs/reconcile", ReconcileRequest{RepoID: "abc"}, &out)
		if err != nil {
			t.Fatalf("Post: %v", err)
		}
		if gotPath != "/api/test-refs/reconcile" || gotType != "application/json" {
			t.Fatalf("path %q type %q", gotPath, gotType)
		}
		if !strings.Contains(gotBody, `"repo_id":"abc"`) || !strings.Contains(gotBody, `"refs":null`) {
			t.Fatalf("body %q", gotBody)
		}
		if out != (ReconcileResponse{Created: 1, Updated: 2, Orphaned: 3}) {
			t.Fatalf("decoded %+v", out)
		}
	})
}

// tdd/unit/scripts/test_cli_errors.py::TestEveryTransportFailureIsHandled
func TestTransportFailures(t *testing.T) {
	ctx := context.Background()
	// noPanic runs f and turns a panic into a test failure: every transport
	// error must become a refusal, never a stack trace.
	noPanic := func(t *testing.T, f func() error) error {
		t.Helper()
		var err error
		func() {
			defer func() {
				if r := recover(); r != nil {
					t.Fatalf("panicked: %v", r)
				}
			}()
			err = f()
		}()
		return err
	}
	t.Run("an unreachable backend names the url and how to check it", func(t *testing.T) {
		// A port nothing listens on: bind one, read its number, close it.
		l, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		base := "http://" + l.Addr().String()
		l.Close()
		c, err := New(base)
		if err != nil {
			t.Fatal(err)
		}
		c.HTTP = &http.Client{Timeout: 5 * time.Second}
		text := rendered(t, noPanic(t, func() error { return c.Get(ctx, "/api/repos", nil) }))
		if !strings.Contains(text, "could not reach the LazyAF backend at "+base) {
			t.Fatalf("refusal:\n%s", text)
		}
		if !strings.Contains(text, "curl "+base+"/health") {
			t.Fatalf("the refusal must say how to check the port:\n%s", text)
		}
		if !strings.Contains(text, "--server") {
			t.Fatalf("the refusal must name where the URL came from:\n%s", text)
		}
	})
	t.Run("a timeout is distinguished from a refusal", func(t *testing.T) {
		// "Not answering" and "not listening" have different causes; one
		// message for both sends the reader to the wrong place.
		release := make(chan struct{})
		c, _ := server(t, func(w http.ResponseWriter, r *http.Request) {
			select {
			case <-release:
			case <-r.Context().Done():
			}
		})
		t.Cleanup(func() { close(release) })
		c.HTTP = &http.Client{Timeout: 100 * time.Millisecond}
		text := rendered(t, noPanic(t, func() error { return c.Get(ctx, "/api/repos", nil) }))
		if !strings.Contains(text, "did not answer within 100ms") {
			t.Fatalf("refusal:\n%s", text)
		}
		if strings.Contains(text, "could not reach") {
			t.Fatalf("a timeout was reported as a refused connection:\n%s", text)
		}
	})
	t.Run("a cancelled context is a refusal too", func(t *testing.T) {
		c, _ := server(t, reply(200, "application/json", "[]"))
		cancelled, cancel := context.WithCancel(ctx)
		cancel()
		if err := noPanic(t, func() error { return c.Get(cancelled, "/api/repos", nil) }); err == nil {
			t.Fatal("a cancelled request succeeded")
		}
	})
	t.Run("a dns failure is a refusal not a traceback", func(t *testing.T) {
		// .invalid is reserved (RFC 2606) and never resolves.
		c, err := New("http://lazyaf-nowhere.invalid:1")
		if err != nil {
			t.Fatal(err)
		}
		c.HTTP = &http.Client{Timeout: 10 * time.Second}
		text := rendered(t, noPanic(t, func() error { return c.Get(ctx, "/api/repos", nil) }))
		if !strings.Contains(text, "Error:") || !strings.Contains(text, "lazyaf-nowhere.invalid") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("a schemeless url is refused before any dial", func(t *testing.T) {
		if _, err := New("h:1"); err == nil {
			t.Fatal("New accepted a schemeless URL")
		}
		// And a Client built by hand around one never reaches the wire.
		c := &Client{HTTP: &http.Client{Transport: panicTransport{}}}
		text := rendered(t, noPanic(t, func() error { return c.Get(ctx, "/api/repos", nil) }))
		if !strings.Contains(text, "no LazyAF backend URL resolved") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
	t.Run("a truncated response is a refusal", func(t *testing.T) {
		c, _ := server(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Length", "100")
			w.WriteHeader(200)
			_, _ = w.Write([]byte("[1,"))
		})
		text := rendered(t, noPanic(t, func() error { return c.Get(ctx, "/api/repos", nil) }))
		if !strings.Contains(text, "Error:") {
			t.Fatalf("refusal:\n%s", text)
		}
	})
}

// panicTransport is the `no_http` fixture: a transport that fails the test
// loudly if a refusal path ever reaches the network.
type panicTransport struct{}

func (panicTransport) RoundTrip(*http.Request) (*http.Response, error) {
	panic("the request reached the transport; it should have been refused before any dial")
}

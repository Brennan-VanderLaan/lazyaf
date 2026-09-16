package cmd

// The test harness for the command tree. Every test here drives the SAME
// path main.go does - NewRootWith + Run - with ui's streams swapped for
// buffers, the process seams (git/gh, the websocket dialer, the console)
// replaced by recorders and fakes, and --server pointed at an
// httptest.Server speaking the real API shapes. Nothing inside the command
// bodies is doubled (the idiom of tdd/unit/scripts/test_cli_errors.py, kept).

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/gitx"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/terminal"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// outcome is one invocation's observable result.
type outcome struct {
	code   int
	stdout string
	stderr string
}

// all is stdout+stderr, for assertions that only care that a sentence was
// said (the Python tests read CliRunner's mixed `output`).
func (o outcome) all() string { return o.stdout + o.stderr }

// run executes `lazyaf args...` over deps and returns what happened.
func run(t *testing.T, deps *Deps, args ...string) outcome {
	t.Helper()
	if deps == nil {
		deps = fakeDeps(t)
	}
	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	savedOut, savedErr := ui.Stdout, ui.Stderr
	ui.Stdout, ui.Stderr = stdout, stderr
	t.Setenv("NO_COLOR", "1")
	defer func() { ui.Stdout, ui.Stderr = savedOut, savedErr }()

	root := NewRootWith(deps)
	root.SetArgs(args)
	code := Run(context.Background(), root)
	return outcome{code: code, stdout: stdout.String(), stderr: stderr.String()}
}

// fakeDeps: a git recorder that answers exit 0 with nothing, a dialer and a
// console that fail the test if reached.
func fakeDeps(t *testing.T) *Deps {
	t.Helper()
	return &Deps{
		Git: &gitx.Recorder{},
		Dial: func(ctx context.Context, url, token string) (terminal.Socket, error) {
			t.Errorf("a websocket was dialed to %s; this path must not open a socket", url)
			return nil, context.Canceled
		},
		Console: func() (terminal.ConsoleIO, func(), string) {
			return terminal.NewPipeConsole(strings.NewReader(""), &bytes.Buffer{}), func() {}, ""
		},
	}
}

// noHTTP is the `no_http` fixture: a backend that fails the test if any
// request reaches it. The refusal under test must happen BEFORE the API is
// called - reaching it would mean server-side state was created before the
// CLI decided to refuse (ingest --branch <typo> used to do exactly that).
func noHTTP(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("this path must refuse BEFORE calling the API; got %s %s", r.Method, r.URL.Path)
		http.Error(w, "unexpected", http.StatusTeapot)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// call is one request the fake backend saw.
type call struct {
	Method string
	Path   string
	Body   string
}

// fakeAPI is a backend whose routes are a map of "METHOD /path" to a
// handler returning (status, JSON body); everything it sees is recorded.
type fakeAPI struct {
	t      *testing.T
	URL    string
	calls  []call
	routes map[string]func(body []byte) (int, any)
}

func newFakeAPI(t *testing.T, routes map[string]func(body []byte) (int, any)) *fakeAPI {
	t.Helper()
	f := &fakeAPI{t: t, routes: routes}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body bytes.Buffer
		_, _ = body.ReadFrom(r.Body)
		f.calls = append(f.calls, call{Method: r.Method, Path: r.URL.Path, Body: body.String()})
		handler, ok := routes[r.Method+" "+r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"no such route in the fake backend: ` + r.Method + " " + r.URL.Path + `"}`))
			return
		}
		status, payload := handler(body.Bytes())
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(payload)
	}))
	t.Cleanup(srv.Close)
	f.URL = srv.URL
	return f
}

// Calls is every request as "METHOD /path", in order.
func (f *fakeAPI) Calls() []string {
	var out []string
	for _, c := range f.calls {
		out = append(out, c.Method+" "+c.Path)
	}
	return out
}

func ok(payload any) func([]byte) (int, any) {
	return func([]byte) (int, any) { return http.StatusOK, payload }
}

// closedPort is a loopback port nothing listens on.
func closedPort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return "http://" + addr
}

func mustContain(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Fatalf("output lacks %q:\n%s", want, text)
		}
	}
}

func mustNotContain(t *testing.T, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if strings.Contains(text, want) {
			t.Fatalf("output must not contain %q:\n%s", want, text)
		}
	}
}

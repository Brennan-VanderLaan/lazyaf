package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/debugproto"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/terminal"
)

// corpusReasons reads constants.reasons out of tdd/contracts/debug_terminal
// .v1.json - data only the SERVER writes (§3.2). test_cli_debug.py:560 pinned
// the CLI's --shell refusal to the server's sentence word for word; that pin
// survives the port only if this test reads the sentence from the corpus
// rather than from a Go constant. A missing corpus is fatal, never a skip.
func corpusReasons(t *testing.T) map[string]string {
	t.Helper()
	path, err := filepath.Abs(filepath.Join("..", "..", "..", "tdd", "contracts", "debug_terminal.v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("the codec corpus is missing at %s (%v); run from a LazyAF checkout", path, err)
	}
	var corpus struct {
		Constants struct {
			Reasons map[string]string `json:"reasons"`
		} `json:"constants"`
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatalf("%s is not the corpus: %v", path, err)
	}
	if corpus.Constants.Reasons["SHELL_REFUSED_REASON"] == "" {
		t.Fatalf("%s has no constants.reasons.SHELL_REFUSED_REASON", path)
	}
	return corpus.Constants.Reasons
}

// sessionAPI is the debug backend the attach tests talk to. Every request
// is recorded so "--print-credential opens no socket" and "--token skips
// the mint" are assertions on calls, not on absence of errors.
func sessionAPI(t *testing.T, attachable bool, reason string) *fakeAPI {
	t.Helper()
	info := map[string]any{
		"id": "sess-1", "pipeline_run_id": "run-1", "status": "waiting_at_breakpoint",
		"attach_available": attachable, "attach_unavailable_reason": nil,
		"breakpoints": []string{"verify"}, "breakpoints_hit": []string{"verify"}, "breakpoints_pending": []string{},
		"commit":  map[string]any{"sha": "abc", "message": "m", "branch": "main"},
		"runtime": map[string]any{"host": "local", "orchestrator": "docker", "image": "python:3.12-slim", "image_sha": nil},
		"logs":    "", "join_command": "lazyaf debug attach sess-1",
	}
	if reason != "" {
		info["attach_unavailable_reason"] = reason
	}
	return newFakeAPI(t, map[string]func([]byte) (int, any){
		"GET /api/debug/sess-1":             ok(info),
		"POST /api/debug/sess-1/join-token": ok(map[string]any{"token": "jwt-value", "expires_at": "2026-08-30T12:00:00", "join_command": "lazyaf debug attach sess-1 --token jwt-value"}),
	})
}

// tdd/unit/scripts/test_cli_debug.py::TestAttachCommand
func TestDebugAttach(t *testing.T) {
	t.Run("shell_is_refused_with_the_reason_never_downgraded", func(t *testing.T) {
		// C17: `--shell` is an error naming why, not a silent fall back to
		// the sidecar - and it refuses BEFORE any HTTP.
		reasons := corpusReasons(t)
		o := run(t, nil, "--server", noHTTP(t), "debug", "attach", "sess-1", "--shell")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, "no step container exists at a pre-step breakpoint", "--sidecar", reasons["SHELL_REFUSED_REASON"])
	})
	t.Run("the_refusal_text_is_the_servers_word_for_word", func(t *testing.T) {
		// One sentence, three surfaces (API, WS close, CLI). Drift here is an
		// operator reading two different explanations of one rule.
		reasons := corpusReasons(t)
		if debugproto.ShellRefusedReason != reasons["SHELL_REFUSED_REASON"] {
			t.Fatalf("debugproto.ShellRefusedReason = %q\ncorpus = %q", debugproto.ShellRefusedReason, reasons["SHELL_REFUSED_REASON"])
		}
		if debugproto.RemoteAttachReason != reasons["REMOTE_ATTACH_REASON"] {
			t.Fatalf("debugproto.RemoteAttachReason = %q\ncorpus = %q", debugproto.RemoteAttachReason, reasons["REMOTE_ATTACH_REASON"])
		}
	})
	t.Run("an_unattachable_session_is_refused_with_the_api_reason", func(t *testing.T) {
		// C16: a remote-step pause says why out loud rather than silently
		// attaching to the wrong volume.
		reason := "terminal attach is not available for steps running on a remote runner"
		api := sessionAPI(t, false, reason)
		o := run(t, nil, "--server", api.URL, "debug", "attach", "sess-1")
		if o.code != 2 {
			t.Fatalf("exit %d, want 2:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, reason, "lazyaf debug status sess-1")
		if got := api.Calls(); len(got) != 1 || got[0] != "GET /api/debug/sess-1" {
			t.Fatalf("calls = %v", got)
		}
	})
	t.Run("print_credential_mints_without_opening_a_socket", func(t *testing.T) {
		api := sessionAPI(t, true, "")
		o := run(t, nil, "--server", api.URL, "debug", "attach", "sess-1", "--print-credential") // fakeDeps' dialer fails the test if called
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		if got := strings.Join(api.Calls(), ","); got != "GET /api/debug/sess-1,POST /api/debug/sess-1/join-token" {
			t.Fatalf("calls = %s", got)
		}
		host := strings.TrimPrefix(api.URL, "http://")
		mustContain(t, o.stdout, "jwt-value", "ws://"+host+"/api/debug/sess-1/terminal?mode=sidecar", Banner)
	})
	t.Run("the_interactive_path_runs_end_to_end_under_the_click_runner", func(t *testing.T) {
		// The whole command body, with only the SOCKET replaced: a scripted
		// socket that plays `closed("resumed")` and validates every outbound
		// frame with the codec the corpus pins. stdin is an empty pipe, which
		// exercises the line-buffered path and its stated warning.
		api := sessionAPI(t, true, "")
		captured := struct{ url, token string }{}
		closed, _ := debugproto.Encode(debugproto.TypeClosed, debugproto.F("reason", "resumed"))
		var sock *terminal.ScriptedSocket
		deps := fakeDeps(t)
		deps.Dial = func(ctx context.Context, url, token string) (terminal.Socket, error) {
			captured.url, captured.token = url, token
			sock = terminal.NewScriptedSocket(closed)
			return sock, nil
		}
		var modeSeen string
		deps.Console = func() (terminal.ConsoleIO, func(), string) {
			c := terminal.NewPipeConsole(strings.NewReader(""), &bytes.Buffer{})
			modeSeen = c.Mode()
			return c, func() {}, ""
		}
		o := run(t, deps, "--server", api.URL, "debug", "attach", "sess-1")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		if !strings.HasSuffix(captured.url, "/api/debug/sess-1/terminal?mode=sidecar") || captured.token != "jwt-value" {
			t.Fatalf("dialed %q with token %q", captured.url, captured.token)
		}
		if modeSeen != terminal.ModeLineBuffered {
			t.Fatalf("console mode = %q", modeSeen)
		}
		mustContain(t, o.stderr, "not a TTY", "resumed", Banner, "console: line-buffered")
		if !sock.Closed() {
			t.Fatal("the socket was not closed after the attach ended")
		}
		if bad := sock.Invalid(); len(bad) != 0 {
			t.Fatalf("the client sent frames the server codec refuses: %v", bad)
		}
	})
	t.Run("token_skips_the_mint_so_the_servers_join_command_is_true", func(t *testing.T) {
		// backend/app/routers/debug.py:119-121 prints `--token <t>`; with it
		// the client must not POST join-token (§5).
		api := sessionAPI(t, true, "")
		closed, _ := debugproto.Encode(debugproto.TypeClosed, debugproto.F("reason", "resumed"))
		var token string
		deps := fakeDeps(t)
		deps.Dial = func(ctx context.Context, url, tok string) (terminal.Socket, error) {
			token = tok
			return terminal.NewScriptedSocket(closed), nil
		}
		o := run(t, deps, "--server", api.URL, "debug", "attach", "sess-1", "--token", "given-token")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		if token != "given-token" {
			t.Fatalf("dialed with %q, want the given token", token)
		}
		for _, c := range api.Calls() {
			if strings.HasSuffix(c, "/join-token") {
				t.Fatalf("--token must skip the mint; calls = %v", api.Calls())
			}
		}
		// And with --print-credential it prints the given one, minting nothing.
		o = run(t, nil, "--server", api.URL, "debug", "attach", "sess-1", "--token", "given-token", "--print-credential")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "token:   given-token", "(supplied with --token)")
	})
	t.Run("a_refused_upgrade_is_exit_1_naming_where_the_reason_can_be_read", func(t *testing.T) {
		api := sessionAPI(t, true, "")
		deps := fakeDeps(t)
		deps.Dial = func(ctx context.Context, url, tok string) (terminal.Socket, error) {
			return nil, &terminal.HandshakeError{Status: 403, Err: context.DeadlineExceeded}
		}
		o := run(t, deps, "--server", api.URL, "debug", "attach", "sess-1", "--token", "bad")
		if o.code != 1 {
			t.Fatalf("exit %d, want 1:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, "403", "lazyaf debug status sess-1")
	})
	t.Run("the_shipped_cli_group_exposes_every_debug_verb", func(t *testing.T) {
		root := NewRootWith(fakeDeps(t))
		debug, _, err := root.Find([]string{"debug"})
		if err != nil {
			t.Fatal(err)
		}
		have := map[string]bool{}
		for _, c := range debug.Commands() {
			have[c.Name()] = true
		}
		for _, verb := range []string{"rerun", "list", "status", "attach", "resume", "abort", "extend"} {
			if !have[verb] {
				t.Errorf("lazyaf debug lacks %q; has %v", verb, have)
			}
		}
	})
}

// The plain-HTTP debug verbs: request shapes (schemas/debug.py) and the
// lines they print, against the fake backend.
func TestDebugVerbs(t *testing.T) {
	var rerunBody, resumeBody, extendBody string
	api := newFakeAPI(t, map[string]func([]byte) (int, any){
		"POST /api/pipeline-runs/run-1/debug-rerun": func(b []byte) (int, any) {
			rerunBody = string(b)
			return 200, map[string]any{"run_id": "run-2", "debug_session_id": "sess-1", "join_command": "lazyaf debug attach sess-1 --token t"}
		},
		"GET /api/debug": ok([]any{
			map[string]any{"id": "sess-1", "status": "waiting_at_breakpoint", "current_step": map[string]any{"key": "verify", "name": "verify", "index": 1, "type": "script"},
				"breakpoints": []string{}, "breakpoints_hit": []string{}, "breakpoints_pending": []string{}, "attach_available": true,
				"commit": map[string]any{"sha": "a", "message": "m", "branch": "b"}, "runtime": map[string]any{"host": "local", "orchestrator": "d", "image": "i"}},
		}),
		"GET /api/debug/sess-1": ok(map[string]any{"id": "sess-1", "pipeline_run_id": "run-2", "status": "waiting_at_breakpoint",
			"current_step": map[string]any{"key": "verify", "name": "verify", "index": 1, "type": "script"},
			"breakpoints":  []string{"build", "verify"}, "breakpoints_hit": []string{"build"}, "breakpoints_pending": []string{"verify"},
			"attach_available": false, "attach_unavailable_reason": debugproto.RemoteAttachReason, "expires_at": "2026-09-16T10:00:00",
			"commit": map[string]any{"sha": "a", "message": "m", "branch": "b"}, "runtime": map[string]any{"host": "remote", "orchestrator": "d", "image": "i"}}),
		"POST /api/debug/sess-1/resume": func(b []byte) (int, any) {
			resumeBody = string(b)
			return 200, map[string]any{"status": "running", "next_breakpoint": "verify"}
		},
		"POST /api/debug/sess-1/abort": ok(map[string]any{"status": "ended", "end_reason": "aborted by user"}),
		"POST /api/debug/sess-1/extend": func(b []byte) (int, any) {
			extendBody = string(b)
			return 200, map[string]any{"expires_at": "2026-09-16T11:00:00", "clamped": true}
		},
	})
	t.Run("rerun sends breakpoints and prints the join command", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "debug", "rerun", "run-1", "--break", "build", "--break", "test", "--timeout", "900")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, rerunBody, `"breakpoints":["build","test"]`, `"use_original_commit":true`, `"timeout_seconds":900`)
		mustNotContain(t, rerunBody, "commit_sha", "branch")
		mustContain(t, o.stdout, "Debug re-run started", "run:     run-2", "session: sess-1", "lazyaf debug attach sess-1 --token t")
		run(t, nil, "--server", api.URL, "debug", "rerun", "run-1", "--commit", "abc")
		mustContain(t, rerunBody, `"use_original_commit":false`, `"commit_sha":"abc"`)
	})
	t.Run("list names the step each session is paused at", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "debug", "list")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "1 active debug session(s)", "  sess-1  waiting_at_breakpoint at verify")
	})
	t.Run("status says why attach is unavailable", func(t *testing.T) {
		// R1: never a silent "no".
		o := run(t, nil, "--server", api.URL, "debug", "status", "sess-1")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, o.stdout, "status:      waiting_at_breakpoint", "run:         run-2", "breakpoints: build, verify",
			"  hit:       build", "  pending:   verify", "paused at:   verify (key verify)", "expires:     2026-09-16T10:00:00",
			"attach:      unavailable - "+debugproto.RemoteAttachReason)
	})
	t.Run("resume abort extend", func(t *testing.T) {
		o := run(t, nil, "--server", api.URL, "debug", "resume", "sess-1", "--all")
		if o.code != 0 {
			t.Fatalf("exit %d:\n%s", o.code, o.all())
		}
		mustContain(t, resumeBody, `"clear_remaining":true`)
		mustContain(t, o.stdout, "Resumed. Session is running; next breakpoint: verify")
		o = run(t, nil, "--server", api.URL, "debug", "abort", "sess-1")
		mustContain(t, o.stdout, "Aborted. Session is ended (aborted by user); the run was cancelled.")
		o = run(t, nil, "--server", api.URL, "debug", "extend", "sess-1", "--minutes", "60")
		mustContain(t, extendBody, `"additional_minutes":60`)
		mustContain(t, o.stdout, "Extended. Expires at 2026-09-16T11:00:00", "Clamped")
	})
	t.Run("a server refusal is quoted and exits 1", func(t *testing.T) {
		refusing := newFakeAPI(t, map[string]func([]byte) (int, any){
			"POST /api/debug/sess-9/resume": func([]byte) (int, any) {
				return http.StatusConflict, map[string]any{"detail": "session already ended (aborted by user)"}
			},
		})
		o := run(t, nil, "--server", refusing.URL, "debug", "resume", "sess-9")
		if o.code != 1 {
			t.Fatalf("exit %d, want 1:\n%s", o.code, o.all())
		}
		mustContain(t, o.stderr, "HTTP 409", "session already ended (aborted by user)")
	})
}

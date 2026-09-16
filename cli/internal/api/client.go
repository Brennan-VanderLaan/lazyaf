package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// Timeout is the per-request budget, the 30 s of cli.py:294.
const Timeout = 30 * time.Second

// Client is one resolved backend. Build it with New; the zero value has no
// base URL and refuses every request.
type Client struct {
	// Base is the validated, slash-stripped backend URL.
	Base string
	// HTTP is the transport. Injectable so a test can shorten the timeout
	// or panic on contact (the `no_http` idiom of the Python tests).
	HTTP *http.Client

	setting ServerSetting
}

// New is NewFromSetting for a plain flag value: explicit when non-empty. It
// cannot tell `--server ""` from an unset flag (see FlagSetting); a caller
// that can, passes the bit through NewFromSetting.
func New(explicit string) (*Client, error) {
	return NewFromSetting(FlagSetting(explicit))
}

// NewFromSetting resolves the backend (flag > env > default) and refuses a
// schemeless or empty setting before any dial.
func NewFromSetting(s ServerSetting) (*Client, error) {
	base, err := ResolveServer(s)
	if err != nil {
		return nil, err
	}
	return &Client{Base: base, HTTP: &http.Client{Timeout: Timeout}, setting: s}, nil
}

// Describe is DescribeSetting for the setting this client was built from.
func (c *Client) Describe() string { return DescribeSetting(c.setting) }

// RequestOption tunes one request.
type RequestOption func(*requestOptions)

type requestOptions struct {
	notFound string
}

// NotFound is the command's own words for a 404 - "repo abc does not exist.
// `lazyaf list` shows the ids that do." - printed above the server's line.
func NotFound(summary string) RequestOption {
	return func(o *requestOptions) { o.notFound = summary }
}

// Get is Request with no body.
func (c *Client) Get(ctx context.Context, path string, out any, opts ...RequestOption) error {
	return c.Request(ctx, http.MethodGet, path, nil, out, opts...)
}

// Post is Request with a JSON body (nil sends none).
func (c *Client) Post(ctx context.Context, path string, body any, out any, opts ...RequestOption) error {
	return c.Request(ctx, http.MethodPost, path, body, out, opts...)
}

// Request is one HTTP call against the LazyAF API, with this package's
// error idiom (cli.py:265-327). body, when non-nil, is sent as JSON; out,
// when non-nil, receives the decoded JSON answer. Every failure - a refusal
// from the server, a transport error, a body that is not JSON - comes back
// as a *ui.Failure naming the URL, quoting the server, and naming the
// remedy; never a panic, never a bare status code.
func (c *Client) Request(ctx context.Context, method, path string, body any, out any, opts ...RequestOption) error {
	var options requestOptions
	for _, opt := range opts {
		opt(&options)
	}
	if c.Base == "" {
		return ui.Fail("no LazyAF backend URL resolved - api.New was not called", ui.WithRemedy(c.Describe()))
	}
	url := c.Base + path

	var payload io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return ui.Fail(fmt.Sprintf("cannot encode the request body for %s %s", method, path), ui.WithDetail(err.Error()))
		}
		payload = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, payload)
	if err != nil {
		return ui.Fail(fmt.Sprintf("the LazyAF backend URL is not a usable URL: %s", c.Base),
			ui.WithDetail(err.Error()), ui.WithRemedy(c.Describe()))
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	httpClient := c.HTTP
	if httpClient == nil {
		httpClient = &http.Client{Timeout: Timeout}
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return c.transportFailure(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return ui.Fail(fmt.Sprintf("the LazyAF backend at %s stopped answering mid-response", c.Base),
			ui.WithDetail(err.Error()), ui.WithRemedy(c.Describe()))
	}

	if resp.StatusCode >= 400 {
		words := serverWords(raw)
		if resp.StatusCode == http.StatusNotFound && options.notFound != "" {
			return ui.Fail(options.notFound, ui.WithDetail(words), ui.WithRemedy(c.Describe()))
		}
		if words == "" {
			words = "(the server sent no explanation)"
		}
		return ui.Fail(
			fmt.Sprintf("the LazyAF backend returned HTTP %d for %s %s", resp.StatusCode, method, path),
			ui.WithDetail(words), ui.WithRemedy(c.Describe()),
		)
	}

	// A 2xx that is not JSON means we are talking to something that is not
	// the LazyAF API - a proxy error page, a dev server, a login wall.
	// Saying "invalid JSON" would blame the wrong component.
	if !json.Valid(raw) {
		return ui.Fail(
			fmt.Sprintf("%s answered %s %s with HTTP %d but the body is not JSON, so this is not the LazyAF API",
				c.Base, method, path, resp.StatusCode),
			ui.WithDetail(truncate(string(raw), 400)), ui.WithRemedy(c.Describe()),
		)
	}
	if out != nil {
		if err := json.Unmarshal(raw, out); err != nil {
			return ui.Fail(
				fmt.Sprintf("%s answered %s %s with JSON this lazyaf does not understand", c.Base, method, path),
				ui.WithDetail(err.Error()+"\n"+truncate(string(raw), 400)),
				ui.WithRemedy(c.Describe()+"\nThe backend and this lazyaf may be different versions."),
			)
		}
	}
	return nil
}

// transportFailure turns every error the transport can raise into a refusal
// (test_cli_errors.py::TestEveryTransportFailureIsHandled). A timeout is
// told apart from a refused connection: "not answering" and "not listening"
// have different causes, and one message for both sends the reader to the
// wrong place.
func (c *Client) transportFailure(err error) error {
	if isTimeout(err) {
		return ui.Fail(
			fmt.Sprintf("the LazyAF backend at %s did not answer within %s", c.Base, timeoutText(c.HTTP)),
			ui.WithDetail(err.Error()),
			ui.WithRemedy(c.Describe()+"\n\nA backend that accepts the connection but never answers is "+
				"usually still starting, or blocked on its database."),
		)
	}
	return ui.Fail(
		fmt.Sprintf("could not reach the LazyAF backend at %s", c.Base),
		ui.WithDetail(err.Error()),
		ui.WithRemedy(fmt.Sprintf("%s\n\nCheck it is up and serving that port:\n    curl %s/health", c.Describe(), c.Base)),
	)
}

func isTimeout(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

func timeoutText(h *http.Client) string {
	if h == nil || h.Timeout == 0 {
		return Timeout.String()
	}
	return h.Timeout.String()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// serverWords is the server's OWN account of a failure (cli.py:218-247).
//
// A status code is not a reason. The API answers 400s and 422s with prose
// that names the remedy ("Repo 'x' has no commits yet ... git push lazyaf
// main"); printing only "API returned 400" throws that away. The pydantic
// 422 envelope [{"loc": [...], "msg": ...}] is rendered as `field: msg`
// lines; a non-JSON body is still the answer and is shown as text.
func serverWords(raw []byte) string {
	var payload any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return strings.TrimSpace(string(raw))
	}
	detail := payload
	if obj, ok := payload.(map[string]any); ok {
		if d, ok := obj["detail"]; ok {
			detail = d
		}
	}
	switch d := detail.(type) {
	case string:
		return d
	case []any:
		var lines []string
		for _, item := range d {
			entry, ok := item.(map[string]any)
			msg, hasMsg := entry["msg"]
			if !ok || !hasMsg {
				lines = append(lines, fmt.Sprint(item))
				continue
			}
			var where []string
			if loc, ok := entry["loc"].([]any); ok {
				for _, part := range loc {
					if s := fmt.Sprint(part); s != "body" {
						where = append(where, s)
					}
				}
			}
			if len(where) > 0 {
				lines = append(lines, fmt.Sprintf("%s: %v", strings.Join(where, "."), msg))
			} else {
				lines = append(lines, fmt.Sprint(msg))
			}
		}
		return strings.Join(lines, "\n")
	default:
		pretty, err := json.MarshalIndent(detail, "", "  ")
		if err != nil {
			return strings.TrimSpace(string(raw))
		}
		return string(pretty)
	}
}

package doctor

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/api"
	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/ui"
)

// backendTimeout is short on purpose: a doctor run must not sit for the
// API client's 30 s on a black-holed --server.
const backendTimeout = 5 * time.Second

// checkBackend is the one check the script did not have (§8.2): GET
// <server>/health through the ONE HTTP client, with the provenance line
// (which of --server / $LAZYAF_SERVER / the default is in use), because
// "which URL" is the most common support question.
//
// A backend that does not answer is a WARN, never a FAIL: doctor is run
// BEFORE `docker compose up`, when nothing is listening yet. A URL that
// cannot be used at all (schemeless, empty) is a FAIL, because every other
// command will refuse it too and the fix is one line.
func checkBackend(r *reporter, opts Options) {
	if opts.Offline {
		r.report(statusOK, "Backend check skipped (--offline)",
			"Re-run without --offline once the stack is up to probe "+strings.TrimSpace(firstLine(api.DescribeServer(opts.Server))))
		return
	}
	client, err := api.New(opts.Server)
	if err != nil {
		var f *ui.Failure
		if errors.As(err, &f) {
			r.report(statusFail, "Backend URL cannot be used: "+f.Summary, f.Remedy)
		} else {
			r.report(statusFail, "Backend URL cannot be used", err.Error())
		}
		return
	}
	client.HTTP.Timeout = backendTimeout
	provenance := firstLine(client.Describe())

	ctx, cancel := context.WithTimeout(context.Background(), backendTimeout)
	defer cancel()
	var health map[string]any
	if err := client.Get(ctx, "/health", &health); err != nil {
		said := err.Error()
		var f *ui.Failure
		if errors.As(err, &f) {
			said = f.Summary
		}
		r.report(statusWarn,
			"No LazyAF backend answering at "+client.Base,
			provenance,
			"Expected before `docker compose up`. If the stack IS running, the URL",
			"above is wrong: change it with --server <url> or "+api.ServerEnvVar+"=<url>.",
			"The client said:",
			"  "+said,
		)
		return
	}
	r.report(statusOK, "Backend at "+client.Base+" answered GET /health", provenance)
}

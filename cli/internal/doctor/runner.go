package doctor

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// defaultTimeout is preflight.py's run() default (:76).
const defaultTimeout = 20 * time.Second

// Runner is the ONE way doctor reaches docker and git: a subprocess
// (preflight.py:76-93). It is an interface so a test can record every
// argv and script every answer - which is how "docker via subprocess only"
// and "pulls nothing, writes nothing, starts nothing" are proven.
type Runner interface {
	// Run executes args[0] with args[1:] and returns (exit code, combined
	// stdout+stderr, trimmed). It never returns an error: a missing
	// program is 127, a timeout 124, anything else that stops the exec 126,
	// each with a one-line explanation as the output.
	Run(timeout time.Duration, args ...string) (int, string)
}

// execRunner is the real thing.
type execRunner struct{}

func (execRunner) Run(timeout time.Duration, args ...string) (int, string) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	out, err := cmd.CombinedOutput()
	text := strings.TrimSpace(string(out))
	if err == nil {
		return 0, text
	}
	var exit *exec.ExitError
	switch {
	case errors.As(err, &exit):
		if ctx.Err() == context.DeadlineExceeded {
			return 124, fmt.Sprintf("timed out after %s: %s", timeout, strings.Join(args, " "))
		}
		return exit.ExitCode(), text
	case errors.Is(err, exec.ErrNotFound):
		return 127, "command not found: " + args[0]
	case ctx.Err() == context.DeadlineExceeded:
		return 124, fmt.Sprintf("timed out after %s: %s", timeout, strings.Join(args, " "))
	default: // permissions, exec format, ...
		return 126, fmt.Sprintf("could not run %s: %v", args[0], err)
	}
}

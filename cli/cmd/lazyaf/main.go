// lazyaf is the LazyAF command-line client. Everything lives in
// cli/internal; this is the exit code and nothing else (upcoming/go-cli.md §2.3).
package main

import (
	"os"

	"github.com/Brennan-VanderLaan/lazyaf/cli/internal/cmd"
)

func main() {
	os.Exit(cmd.Execute())
}

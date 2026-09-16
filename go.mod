module github.com/Brennan-VanderLaan/lazyaf

// upcoming/go-cli.md §2.2: x/term v0.46.0 declares go 1.26.0, so the
// directive cannot be lower; the toolchain line is what the test-runner
// image bakes (GOTOOLCHAIN=local there) and what a go1.21 dev box downloads
// once under GOTOOLCHAIN=auto.
go 1.26.0

toolchain go1.26.8

require (
	github.com/coder/websocket v1.8.15
	github.com/spf13/cobra v1.10.2
	golang.org/x/sys v0.48.0
	golang.org/x/term v0.46.0
)

require (
	github.com/bitfield/gotestdox v0.2.2 // indirect
	github.com/dnephin/pflag v1.0.7 // indirect
	github.com/fatih/color v1.18.0 // indirect
	github.com/fsnotify/fsnotify v1.9.0 // indirect
	github.com/google/shlex v0.0.0-20191202100458-e7afc7fbc510 // indirect
	github.com/inconshreveable/mousetrap v1.1.0 // indirect
	github.com/mattn/go-colorable v0.1.13 // indirect
	github.com/mattn/go-isatty v0.0.20 // indirect
	github.com/spf13/pflag v1.0.9 // indirect
	golang.org/x/mod v0.27.0 // indirect
	golang.org/x/sync v0.17.0 // indirect
	golang.org/x/text v0.17.0 // indirect
	golang.org/x/tools v0.36.0 // indirect
	gotest.tools/gotestsum v1.13.0 // indirect
)

// Test tool only (§2.4): run as `go tool gotestsum`; never linked into the
// binary. Exists because scripts/ci_gate.py reads junit <testcase> elements.
tool gotest.tools/gotestsum

// Package parity holds Enforcement B of the parity ledger
// (upcoming/go-cli.md §11): a test, and nothing else. The ledger in
// tdd/contracts/cli_parity.json names the Go tests that carry every property
// the retired Python CLI tests pinned; ledger_test.go proves each named test
// still exists, with go/parser over every *_test.go under cli/, so a carrier
// cannot be deleted without editing the ledger. Nothing links this package
// into the binary; it exists so `go test ./cli/internal/parity/` is a real
// package with a stated purpose rather than a directory of loose test files.
package parity

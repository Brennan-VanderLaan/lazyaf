package api

import (
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// tdd/unit/scripts/test_cli_errors.py::TestOneErrorIdiom, enforced by AST
// instead of by grepping for `httpx`: the ONE HTTP client lives here, and
// the ONE websocket dialer lives in internal/terminal (§2.3, R3).
//
// Non-test files only, stated: a _test.go may import net/http/httptest to
// stand up a fake server, and terminal's read-limit test needs a real
// websocket peer. The property guards the shipped binary, not its tests.
func TestOnlyAPIImportsNetHTTP(t *testing.T) {
	root, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "api")); err != nil {
		t.Fatalf("%s is not cli/internal (no api/ under it): %v", root, err)
	}
	owners := map[string]string{
		"net/http":                   "api",
		"github.com/coder/websocket": "terminal",
	}
	seen := map[string]bool{}
	fset := token.NewFileSet()
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		pkg := filepath.ToSlash(filepath.Dir(rel))
		file, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		for _, imp := range file.Imports {
			name, _ := strconv.Unquote(imp.Path.Value)
			owner, restricted := owners[name]
			if !restricted {
				continue
			}
			seen[name] = true
			if pkg != owner {
				t.Errorf("%s imports %s; only internal/%s may (R3: one idiom for one question)", rel, name, owner)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for name, owner := range owners {
		if !seen[name] {
			t.Errorf("no non-test file under internal/%s imports %s - the rule guards nothing", owner, name)
		}
	}
}

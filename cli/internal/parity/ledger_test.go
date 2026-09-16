package parity

// Enforcement B of the parity ledger (upcoming/go-cli.md §11, R2).
//
// tdd/contracts/cli_parity.json records where every Python CLI test went:
// to named Go tests ("go") or out of the suite with a reason ("retired").
// Enforcement A (tdd/unit/scripts/test_cli_parity_ledger.py) checks the
// Python side while the Python files exist; it is deleted with them. THIS
// test is permanent: it parses every *_test.go under cli/ with go/parser
// (a real AST, not a regex) and refuses a ledger reference that no longer
// resolves, so a Go test that is the parity carrier for a Python class
// cannot be deleted later without editing the ledger and saying why.
//
// What "resolves" means, stated because both enforcements must agree:
//   - "<package>.<TestFunc>": package is the DIRECTORY under cli/internal/
//     (reconcile's external `package reconcile_test` is keyed reconcile),
//     TestFunc is a top-level `func TestX(t *testing.T)`; TestMain is not one;
//   - "<package>.<TestFunc>/<subtest>": a `t.Run("...")` call whose first
//     argument is a string LITERAL, somewhere inside that function's body,
//     compared after the rewrite Go's testing package applies to subtest
//     names (space -> "_"), which is also the spelling gotestsum's junit
//     reports and `go test -run` matches;
//   - a Go test named inside a retired reason ("debugproto.TestContractFrames")
//     must resolve too, so a retirement never points at a mechanism that has
//     since been removed.
//
// The checker is a pure function over the loaded ledger and the index, and
// the last test drives it with a ledger built to be wrong (R4).

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// The closed set of retirement kinds (§11). Held in the two enforcement tests,
// NOT in the JSON, so the set cannot be widened by editing the ledger.
var retirementKinds = map[string]bool{
	"rich-specific":              true,
	"click-specific":             true,
	"python-websockets-specific": true,
	"packaging-wheel":            true,
	"python-interpreter":         true,
	"semantics-changed":          true,
	"superseded-by-contract":     true,
	"moved-to-e2e":               true,
}

const minReasonChars = 40

var (
	goRef         = regexp.MustCompile(`^([a-z][a-z0-9_]*)\.(Test[A-Za-z0-9_]+)(?:/([^/]+))?$`)
	goNameInProse = regexp.MustCompile(`\b([a-z][a-z0-9_]*)\.(Test[A-Za-z0-9_]+)\b`)
	tbd           = regexp.MustCompile(`\bTBD\b|\bTODO\b`)
)

type entry struct {
	Go      []string `json:"go"`
	Retired string   `json:"retired"`
	Note    string   `json:"note"`
	// raw keeps the field names actually present so "go": [] and a missing
	// "go" are told apart, and unknown fields are refused.
	raw map[string]json.RawMessage
}

func (e *entry) UnmarshalJSON(b []byte) error {
	type plain entry
	var p plain
	if err := json.Unmarshal(b, &p); err != nil {
		return err
	}
	*e = entry(p)
	return json.Unmarshal(b, &e.raw)
}

type ledger struct {
	Entries map[string]entry `json:"entries"`
}

// index is {package: {TestFunc: {subtest: true}}}.
type index map[string]map[string]map[string]bool

// repoRoot is three levels up from this package (cli/internal/parity), and
// go.mod must be there - otherwise this test is not running from a checkout.
func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("%s has no go.mod; run the Go suite from a LazyAF checkout: %v", root, err)
	}
	return root
}

func loadLedger(t *testing.T, path string) ledger {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		// Never t.Skip: a missing ledger means the gate has nothing to check,
		// which is a failure of the tree, not a reason to pass.
		t.Fatalf("parity ledger missing at %s (run from a LazyAF checkout): %v", path, err)
	}
	var l ledger
	if err := json.Unmarshal(data, &l); err != nil {
		t.Fatalf("%s is not the ledger shape: %v", path, err)
	}
	if len(l.Entries) == 0 {
		t.Fatalf("%s has no entries", path)
	}
	return l
}

// rewrite is testing.(*common).rewrite's visible effect on subtest names:
// each space becomes an underscore. Non-printables are also escaped there,
// but no ledger name contains one.
func rewrite(name string) string { return strings.ReplaceAll(name, " ", "_") }

// buildIndex parses every *_test.go under cliRoot. The package key is the
// directory under cli/internal/ (or the path relative to cli/ otherwise).
func buildIndex(t *testing.T, cliRoot string) index {
	t.Helper()
	idx := index{}
	fset := token.NewFileSet()
	err := filepath.WalkDir(cliRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, "_test.go") {
			return nil
		}
		rel, err := filepath.Rel(cliRoot, path)
		if err != nil {
			return err
		}
		parts := strings.Split(filepath.ToSlash(filepath.Dir(rel)), "/")
		pkg := filepath.ToSlash(filepath.Dir(rel))
		if len(parts) >= 2 && parts[0] == "internal" {
			pkg = parts[len(parts)-1]
		}
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return fmt.Errorf("%s: %w", rel, err)
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Recv != nil || !isTestFunc(fn) {
				continue
			}
			if idx[pkg] == nil {
				idx[pkg] = map[string]map[string]bool{}
			}
			subs := idx[pkg][fn.Name.Name]
			if subs == nil {
				subs = map[string]bool{}
				idx[pkg][fn.Name.Name] = subs
			}
			ast.Inspect(fn.Body, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || len(call.Args) < 2 {
					return true
				}
				sel, ok := call.Fun.(*ast.SelectorExpr)
				if !ok || sel.Sel.Name != "Run" {
					return true
				}
				lit, ok := call.Args[0].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					return true // table-driven: t.Run(tc.name, ...) is not a literal
				}
				name, err := strconv.Unquote(lit.Value)
				if err == nil {
					subs[rewrite(name)] = true
				}
				return true
			})
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return idx
}

// isTestFunc: `func TestX(t *testing.T)` exactly - one parameter of type
// *testing.T. TestMain(m *testing.M) and helpers are excluded by shape.
func isTestFunc(fn *ast.FuncDecl) bool {
	if !strings.HasPrefix(fn.Name.Name, "Test") || fn.Type.Params == nil || len(fn.Type.Params.List) != 1 {
		return false
	}
	star, ok := fn.Type.Params.List[0].Type.(*ast.StarExpr)
	if !ok {
		return false
	}
	sel, ok := star.X.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	pkg, ok := sel.X.(*ast.Ident)
	return ok && pkg.Name == "testing" && sel.Sel.Name == "T"
}

// resolve returns "" when ref names a real Go test, else why it does not.
func resolve(ref string, idx index) string {
	m := goRef.FindStringSubmatch(ref)
	if m == nil {
		return fmt.Sprintf("%q is not <package>.<TestFunc>[/<subtest>]", ref)
	}
	pkg, fn, sub := m[1], m[2], m[3]
	if idx[pkg] == nil {
		return fmt.Sprintf("%s: no *_test.go under cli/internal/%s", ref, pkg)
	}
	if idx[pkg][fn] == nil {
		return fmt.Sprintf("%s: no `func %s(t *testing.T)` in cli/internal/%s", ref, fn, pkg)
	}
	if sub != "" && !idx[pkg][fn][sub] {
		known := make([]string, 0, len(idx[pkg][fn]))
		for k := range idx[pkg][fn] {
			known = append(known, k)
		}
		sort.Strings(known)
		return fmt.Sprintf("%s: no t.Run(%q) literal inside %s.%s (known: %v)", ref, sub, pkg, fn, known)
	}
	return ""
}

// problems is every way the ledger can be wrong against this tree. The
// message for a dangling carrier is the one §11 prescribes: it names the
// Python test the Go test stands in for and the two ways out.
func problems(l ledger, idx index) []string {
	var out []string
	keys := make([]string, 0, len(l.Entries))
	for k := range l.Entries {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, key := range keys {
		e := l.Entries[key]
		if !strings.Contains(key, "::") {
			out = append(out, fmt.Sprintf("key %q is not file::Name", key))
		}
		for field := range e.raw {
			switch field {
			case "go", "retired", "note":
			default:
				out = append(out, fmt.Sprintf("%s: unknown field %q", key, field))
			}
		}
		_, hasGo := e.raw["go"]
		_, hasRetired := e.raw["retired"]
		if hasGo == hasRetired {
			out = append(out, fmt.Sprintf("%s: needs exactly one of 'go' or 'retired'", key))
			continue
		}
		if hasGo {
			if len(e.Go) == 0 {
				out = append(out, fmt.Sprintf("%s: 'go' must be a non-empty list", key))
				continue
			}
			seen := map[string]bool{}
			for _, ref := range e.Go {
				if seen[ref] {
					out = append(out, fmt.Sprintf("%s: duplicate go reference %s", key, ref))
				}
				seen[ref] = true
				if why := resolve(ref, idx); why != "" {
					out = append(out, fmt.Sprintf(
						"%s is the parity carrier for %s; retire it in tdd/contracts/cli_parity.json with a reason or restore it (%s)",
						ref, key, why))
				}
			}
			continue
		}
		kind, rest, ok := strings.Cut(e.Retired, ": ")
		if !ok || !retirementKinds[kind] {
			out = append(out, fmt.Sprintf("%s: retired kind %q is not in the closed set (the value must read '<kind>: <reason>')", key, kind))
		}
		if len(strings.TrimSpace(rest)) < minReasonChars {
			out = append(out, fmt.Sprintf("%s: the retirement reason is under %d characters", key, minReasonChars))
		}
		if tbd.MatchString(e.Retired) {
			out = append(out, fmt.Sprintf("%s: a retirement reason may not be TBD/TODO", key))
		}
		for _, m := range goNameInProse.FindAllStringSubmatch(e.Retired, -1) {
			if why := resolve(m[1]+"."+m[2], idx); why != "" {
				out = append(out, fmt.Sprintf("%s: the reason names a Go test that does not exist - %s", key, why))
			}
		}
	}
	return out
}

func TestLedgerReferencesResolve(t *testing.T) {
	root := repoRoot(t)
	l := loadLedger(t, filepath.Join(root, "tdd", "contracts", "cli_parity.json"))
	idx := buildIndex(t, filepath.Join(root, "cli"))
	if len(idx) == 0 {
		t.Fatalf("no *_test.go under %s", filepath.Join(root, "cli"))
	}

	found := problems(l, idx)
	for _, p := range found {
		t.Error(p)
	}

	// The share, printed into the test log (§11 "prints the retired share").
	ported, retired := 0, 0
	kinds := map[string]int{}
	for _, e := range l.Entries {
		if _, ok := e.raw["retired"]; ok {
			retired++
			kind, _, _ := strings.Cut(e.Retired, ": ")
			kinds[kind]++
		} else {
			ported++
		}
	}
	names := make([]string, 0, len(kinds))
	for k := range kinds {
		names = append(names, fmt.Sprintf("%s %d", k, kinds[k]))
	}
	sort.Strings(names)
	t.Logf("cli_parity.json: %d entries - %d go, %d retired (%.0f%%): %s",
		len(l.Entries), ported, retired, 100*float64(retired)/float64(len(l.Entries)), strings.Join(names, ", "))
	if ported <= retired {
		t.Errorf("more Python tests were retired (%d) than ported (%d); that is not a port", retired, ported)
	}
}

func TestIndexReadsTheRealTree(t *testing.T) {
	root := repoRoot(t)
	idx := buildIndex(t, filepath.Join(root, "cli"))

	t.Run("this test is in its own index", func(t *testing.T) {
		if !idx["parity"]["TestIndexReadsTheRealTree"]["this_test_is_in_its_own_index"] {
			t.Fatalf("parity.TestIndexReadsTheRealTree/this_test_is_in_its_own_index not indexed: %v", idx["parity"])
		}
	})
	t.Run("an external test package is keyed by its directory", func(t *testing.T) {
		if idx["reconcile"] == nil || idx["reconcile"]["TestFromCollect"] == nil {
			t.Fatalf("reconcile (package reconcile_test) not keyed by directory: %v", keysOf(idx))
		}
	})
	t.Run("TestMain is not a test", func(t *testing.T) {
		if idx["initcmd"] == nil {
			t.Fatal("no initcmd package indexed")
		}
		if idx["initcmd"]["TestMain"] != nil {
			t.Fatal("TestMain(m *testing.M) was indexed as a test")
		}
	})
	t.Run("subtest names are rewritten like the testing package", func(t *testing.T) {
		if !idx["terminal"]["TestTerminalURL"]["http_becomes_ws"] {
			t.Fatalf(`t.Run("http becomes ws") should index as http_becomes_ws; got %v`, idx["terminal"]["TestTerminalURL"])
		}
	})
}

func TestCheckerCatchesEachFailureShape(t *testing.T) {
	idx := index{"cmd": {"TestLand": {"a_failed_pr_does_not_report_success": true}}}
	good := func() ledger {
		var l ledger
		if err := json.Unmarshal([]byte(`{"entries": {
			"x/test_a.py::TestOne": {"go": ["cmd.TestLand", "cmd.TestLand/a_failed_pr_does_not_report_success"]},
			"x/test_a.py::TestTwo": {"retired": "rich-specific: no markup parser exists in the binary at all any more"}
		}}`), &l); err != nil {
			t.Fatal(err)
		}
		return l
	}
	if got := problems(good(), idx); len(got) != 0 {
		t.Fatalf("a correct ledger reported problems: %v", got)
	}
	cases := []struct {
		name  string
		entry string
		want  string
	}{
		{"a deleted carrier is named with the Python test and the way out",
			`{"go": ["cmd.TestNope"]}`,
			"cmd.TestNope is the parity carrier for x/test_a.py::TestOne; retire it in tdd/contracts/cli_parity.json with a reason or restore it"},
		{"a deleted subtest is named",
			`{"go": ["cmd.TestLand/gone"]}`,
			`no t.Run("gone") literal inside cmd.TestLand`},
		{"an unknown package is named",
			`{"go": ["nowhere.TestLand"]}`,
			"no *_test.go under cli/internal/nowhere"},
		{"an empty go list is refused",
			`{"go": []}`,
			"'go' must be a non-empty list"},
		{"both go and retired is refused",
			`{"go": ["cmd.TestLand"], "retired": "rich-specific: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}`,
			"needs exactly one of 'go' or 'retired'"},
		{"a kind outside the closed set is refused",
			`{"retired": "just-because: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}`,
			`retired kind "just-because" is not in the closed set`},
		{"a short reason is refused",
			`{"retired": "rich-specific: too short"}`,
			"under 40 characters"},
		{"a TBD is refused",
			`{"retired": "rich-specific: TBD - decide once the port is finished and reviewed"}`,
			"may not be TBD/TODO"},
		{"a reason naming a missing Go test is refused",
			`{"retired": "semantics-changed: replaced by cmd.TestImaginary which covers the same ground"}`,
			"the reason names a Go test that does not exist"},
		{"an unknown field is refused",
			`{"go": ["cmd.TestLand"], "todo": "later"}`,
			`unknown field "todo"`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			l := good()
			var e entry
			if err := json.Unmarshal([]byte(tc.entry), &e); err != nil {
				t.Fatal(err)
			}
			l.Entries["x/test_a.py::TestOne"] = e
			found := problems(l, idx)
			for _, p := range found {
				if strings.Contains(p, tc.want) {
					return
				}
			}
			t.Fatalf("expected a problem containing %q; got %v", tc.want, found)
		})
	}
}

func keysOf(idx index) []string {
	out := make([]string, 0, len(idx))
	for k := range idx {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

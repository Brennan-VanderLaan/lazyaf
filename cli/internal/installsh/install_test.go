// Package installsh runs cli/install.sh against a local dist (upcoming/go-cli.md
// §6 "Tested"). It is a Go test so it runs in the TG tier with everything else
// and counts in the tier's floor; the thing under test is bash.
//
// The dist is built ONCE in TestMain with `go build` from ../../cmd/lazyaf,
// stamped Version=1.2.3 through the same -X flag scripts/build_cli.sh uses, so
// the installer's post-install proof (`--version --short` == the version in the
// asset name) is exercised for real rather than with a stub binary. A second
// build stamped 4.5.6 plays the impostor: correct checksum, wrong version.
//
// Needs bash, sha256sum or shasum, and go on the host. Present in the
// test-runner image (Debian) and on the owner's box (Git Bash). Absent →
// the suite fails naming the tool; never t.Skip (R4).
package installsh

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const (
	goodVersion     = "1.2.3"
	impostorVersion = "4.5.6"
	versionPkg      = "github.com/Brennan-VanderLaan/lazyaf/cli/internal/version"
)

var (
	bashPath   string // the bash that runs install.sh
	scriptPath string // absolute path of cli/install.sh
	distDir    string // the dist install.sh reads: one host binary + checksums.txt
	impostor   string // a binary stamped impostorVersion, same name as the good one
	assetName  string // lazyaf_1.2.3_<goos>_<goarch>[.exe]
	exeSuffix  string
)

func TestMain(m *testing.M) {
	if err := setup(); err != nil {
		fmt.Fprintf(os.Stderr, "installsh: cannot set up the dist for install.sh: %v\n", err)
		os.Exit(1)
	}
	code := m.Run()
	os.RemoveAll(filepath.Dir(distDir))
	os.Exit(code)
}

func setup() error {
	var err error
	if bashPath, err = findBash(); err != nil {
		return err
	}
	if scriptPath, err = filepath.Abs(filepath.Join("..", "..", "install.sh")); err != nil {
		return err
	}
	if _, err = os.Stat(scriptPath); err != nil {
		return fmt.Errorf("%s: %w (run from a LazyAF checkout)", scriptPath, err)
	}
	root, err := os.MkdirTemp("", "lazyaf-installsh-")
	if err != nil {
		return err
	}
	if runtime.GOOS == "windows" {
		exeSuffix = ".exe"
	}
	assetName = fmt.Sprintf("lazyaf_%s_%s_%s%s", goodVersion, runtime.GOOS, runtime.GOARCH, exeSuffix)
	distDir = filepath.Join(root, "dist")
	if err := os.Mkdir(distDir, 0o755); err != nil {
		return err
	}
	good := filepath.Join(distDir, assetName)
	if err := buildBinary(good, goodVersion); err != nil {
		return err
	}
	impostor = filepath.Join(root, "impostor"+exeSuffix)
	if err := buildBinary(impostor, impostorVersion); err != nil {
		return err
	}
	return writeChecksums(distDir, assetName)
}

// buildBinary is scripts/build_cli.sh's host build in miniature: the same -X
// on the same package, so the installed binary really reports the version
// the asset name promises.
func buildBinary(out, version string) error {
	cmd := exec.Command("go", "build", "-trimpath",
		"-ldflags", "-s -w -X "+versionPkg+".Version="+version,
		"-o", out, "../../cmd/lazyaf")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if b, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("go build %s: %v\n%s", out, err, b)
	}
	return nil
}

// writeChecksums writes checksums.txt in sha256sum's two-space format for
// every lazyaf_* file in dir - what build_cli.sh writes and install.sh reads.
func writeChecksums(dir string, names ...string) error {
	var sb strings.Builder
	for _, name := range names {
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		fmt.Fprintf(&sb, "%x  %s\n", sha256.Sum256(b), name)
	}
	return os.WriteFile(filepath.Join(dir, "checksums.txt"), []byte(sb.String()), 0o644)
}

// findBash locates the bash that will run the script. On Windows the first
// `bash` on PATH may be the WSL launcher in System32, which cannot run a Git
// Bash script against Windows paths; Git for Windows' own bash is what the
// owner uses and what §6 targets, so that one is preferred.
func findBash() (string, error) {
	if p := os.Getenv("LAZYAF_TEST_BASH"); p != "" {
		return p, nil
	}
	found, lookErr := exec.LookPath("bash")
	if runtime.GOOS != "windows" {
		if lookErr != nil {
			return "", errors.New("bash is not on PATH; install.sh is a bash script (apt-get install bash)")
		}
		return found, nil
	}
	if lookErr == nil && !strings.Contains(strings.ToLower(found), `\windows\`) {
		return found, nil
	}
	if git, err := exec.LookPath("git"); err == nil {
		gitRoot := filepath.Dir(filepath.Dir(git)) // <root>\cmd\git.exe -> <root>
		for _, candidate := range []string{
			filepath.Join(gitRoot, "bin", "bash.exe"),
			filepath.Join(gitRoot, "usr", "bin", "bash.exe"),
		} {
			if _, err := os.Stat(candidate); err == nil {
				return candidate, nil
			}
		}
	}
	return "", errors.New("no Git Bash found (only the WSL launcher, or none); install Git for Windows or set LAZYAF_TEST_BASH to its bin\\bash.exe")
}

// posixPath is what bash on this host calls p: on Windows, Git Bash's
// /c/Users/... spelling via cygpath (so the script's every path stays a
// plain string it can quote), elsewhere p itself.
func posixPath(t *testing.T, p string) string {
	t.Helper()
	if runtime.GOOS != "windows" {
		return p
	}
	out, err := exec.Command(bashPath, "-c", `cygpath -u "$1"`, "_", p).Output()
	if err != nil {
		t.Fatalf("cygpath -u %q: %v", p, err)
	}
	return strings.TrimSpace(string(out))
}

// windowsPath is what Windows (Settings, PowerShell) calls p: the cygpath -w
// spelling of the canonical directory, which is what install.sh's step 8
// must print for a custom target - the verifiers caught it printing
// %USERPROFILE%\.local\bin for a directory that held nothing.
func windowsPath(t *testing.T, posix string) string {
	t.Helper()
	out, err := exec.Command(bashPath, "-c", `cygpath -w "$1"`, "_", posix).Output()
	if err != nil {
		t.Fatalf("cygpath -w %q: %v", posix, err)
	}
	return strings.TrimSpace(string(out))
}

type result struct {
	code   int
	output string // stdout + stderr interleaved, as a person sees it
}

// runInstall runs install.sh with LAZYAF_* scrubbed from the environment and
// the given overrides (KEY=VALUE) applied on top.
func runInstall(t *testing.T, env []string, args ...string) result {
	t.Helper()
	cmd := exec.Command(bashPath, append([]string{scriptPath}, args...)...)
	cmd.Env = childEnv(env)
	out, err := cmd.CombinedOutput()
	res := result{output: string(out)}
	var exitErr *exec.ExitError
	switch {
	case err == nil:
	case errors.As(err, &exitErr):
		res.code = exitErr.ExitCode()
	default:
		t.Fatalf("running %s: %v\n%s", scriptPath, err, out)
	}
	t.Logf("install.sh %s -> exit %d\n%s", strings.Join(args, " "), res.code, res.output)
	return res
}

func childEnv(overrides []string) []string {
	drop := map[string]bool{}
	for _, kv := range overrides {
		drop[strings.ToUpper(kv[:strings.Index(kv, "=")])] = true
	}
	var env []string
	for _, kv := range os.Environ() {
		key := strings.ToUpper(kv[:strings.Index(kv, "=")])
		if strings.HasPrefix(key, "LAZYAF_") || drop[key] {
			continue
		}
		env = append(env, kv)
	}
	return append(env, overrides...)
}

// canonPath is the spelling install.sh prints for a directory: it
// canonicalises its target with `cd && pwd -P`, which under Git Bash turns
// /tmp/... and 8.3 short names into the real /c/Users/... path. The
// directory is created first so there is something to resolve.
func canonPath(t *testing.T, dir string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(bashPath, "-c", `cd "$1" && pwd -P`, "_", posixPath(t, dir)).Output()
	if err != nil {
		t.Fatalf("canonical path of %q: %v", dir, err)
	}
	return strings.TrimSpace(string(out))
}

// installDir is a fresh target directory: its native path, the spelling
// passed to --install-dir, and the canonical spelling the script prints.
func installDir(t *testing.T) (native, posix, canon string) {
	t.Helper()
	native = filepath.Join(t.TempDir(), "bin")
	return native, posixPath(t, native), canonPath(t, native)
}

func mustNotHaveTmp(t *testing.T, dir string) {
	t.Helper()
	leftovers, _ := filepath.Glob(filepath.Join(dir, "lazyaf.tmp.*"))
	if len(leftovers) != 0 {
		t.Fatalf("install.sh left a temp file behind: %v", leftovers)
	}
}

func installedVersion(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command(filepath.Join(dir, "lazyaf"+exeSuffix), "--version", "--short").Output()
	if err != nil {
		t.Fatalf("installed binary --version --short: %v", err)
	}
	return strings.TrimSpace(string(out))
}

// copyDist duplicates the shared dist into a temp dir the test may corrupt.
func copyDist(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "dist")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{assetName, "checksums.txt"} {
		b, err := os.ReadFile(filepath.Join(distDir, name))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), b, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestScriptParses(t *testing.T) {
	// `bash -n` is what pr-build runs; shellcheck runs there too (ubuntu) and
	// in the tier image, but is not required on a dev box, so it is not
	// invoked here - a missing tool must not turn into a passing test.
	if out, err := exec.Command(bashPath, "-n", scriptPath).CombinedOutput(); err != nil {
		t.Fatalf("bash -n %s: %v\n%s", scriptPath, err, out)
	}
}

func TestInstallsVerifiesAndProves(t *testing.T) {
	native, posix, canon := installDir(t)
	res := runInstall(t, nil, "--from-dir", posixPath(t, distDir), "--install-dir", posix, "--no-completions")
	if res.code != 0 {
		t.Fatalf("exit %d, want 0", res.code)
	}
	for _, want := range []string{
		"installing lazyaf v" + goodVersion,
		"verified sha256 ",
		"installed " + canon + "/lazyaf" + exeSuffix + " (lazyaf " + goodVersion + ")",
	} {
		if !strings.Contains(res.output, want) {
			t.Errorf("output lacks %q", want)
		}
	}
	if got := installedVersion(t, native); got != goodVersion {
		t.Errorf("installed binary reports %q, want %q", got, goodVersion)
	}
	mustNotHaveTmp(t, native)
}

func TestCorruptedByteIsRefusedBeforeAnythingIsInstalled(t *testing.T) {
	dist := copyDist(t)
	path := filepath.Join(dist, assetName)
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	b[len(b)/2] ^= 0xff // one byte, deep in the code, checksum still claims the original
	if err := os.WriteFile(path, b, 0o755); err != nil {
		t.Fatal(err)
	}

	native, posix, _ := installDir(t)
	res := runInstall(t, nil, "--from-dir", posixPath(t, dist), "--install-dir", posix, "--no-completions")
	if res.code != 1 {
		t.Fatalf("exit %d, want 1", res.code)
	}
	for _, want := range []string{"sha256 mismatch for " + assetName, "expected ", "actual   ", "nothing was installed"} {
		if !strings.Contains(res.output, want) {
			t.Errorf("output lacks %q", want)
		}
	}
	if _, err := os.Stat(filepath.Join(native, "lazyaf"+exeSuffix)); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("a corrupted binary was installed anyway (stat err = %v)", err)
	}
	mustNotHaveTmp(t, native)
}

func TestUnknownPlatformRefusesNamingTheMatrix(t *testing.T) {
	_, posix, _ := installDir(t)
	res := runInstall(t, []string{"LAZYAF_FORCE_PLATFORM=plan9/mips"},
		"--from-dir", posixPath(t, distDir), "--install-dir", posix, "--no-completions")
	if res.code != 1 {
		t.Fatalf("exit %d, want 1", res.code)
	}
	for _, want := range []string{
		"no prebuilt lazyaf for plan9/mips",
		"linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64", "windows/amd64", "windows/arm64",
		"go install github.com/Brennan-VanderLaan/lazyaf/cli/cmd/lazyaf@latest",
	} {
		if !strings.Contains(res.output, want) {
			t.Errorf("output lacks %q", want)
		}
	}
	// The pinned form names the tag, which is what `go install` needs.
	res = runInstall(t, []string{"LAZYAF_FORCE_PLATFORM=plan9/mips", "LAZYAF_VERSION=v0.3.0"},
		"--from-dir", posixPath(t, distDir), "--install-dir", posix, "--no-completions")
	if res.code != 1 || !strings.Contains(res.output, "lazyaf/cli/cmd/lazyaf@v0.3.0") {
		t.Errorf("pinned refusal should name @v0.3.0: exit %d\n%s", res.code, res.output)
	}
}

func TestSecondRunSaysReinstalling(t *testing.T) {
	native, posix, _ := installDir(t)
	args := []string{"--from-dir", posixPath(t, distDir), "--install-dir", posix, "--no-completions"}
	if res := runInstall(t, nil, args...); res.code != 0 {
		t.Fatalf("first run: exit %d", res.code)
	}
	res := runInstall(t, nil, args...)
	if res.code != 0 {
		t.Fatalf("second run: exit %d, want 0 (reinstall is idempotent)", res.code)
	}
	if want := goodVersion + " is already installed; reinstalling"; !strings.Contains(res.output, want) {
		t.Errorf("second run lacks %q", want)
	}
	if got := installedVersion(t, native); got != goodVersion {
		t.Errorf("after reinstall the binary reports %q", got)
	}
	mustNotHaveTmp(t, native)
}

func TestUpgradeReportsOldArrowNew(t *testing.T) {
	// An older lazyaf (the impostor, which says 4.5.6) is already in place;
	// the report reads `lazyaf 4.5.6 -> 1.2.3`.
	native, posix, _ := installDir(t)
	b, err := os.ReadFile(impostor)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(native, "lazyaf"+exeSuffix), b, 0o755); err != nil {
		t.Fatal(err)
	}
	res := runInstall(t, nil, "--from-dir", posixPath(t, distDir), "--install-dir", posix, "--no-completions")
	if res.code != 0 {
		t.Fatalf("exit %d, want 0", res.code)
	}
	if want := "lazyaf " + impostorVersion + " -> " + goodVersion; !strings.Contains(res.output, want) {
		t.Errorf("output lacks %q", want)
	}
	if got := installedVersion(t, native); got != goodVersion {
		t.Errorf("after upgrade the binary reports %q", got)
	}
}

func TestPostInstallProofCatchesAWrongBinary(t *testing.T) {
	// Correct checksum, wrong contents: the asset named 1.2.3 is the 4.5.6
	// build. The checksum proves bytes; §6 step 7 proves the bytes RUN and
	// say what the name promised.
	dist := copyDist(t)
	b, err := os.ReadFile(impostor)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dist, assetName), b, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeChecksums(dist, assetName); err != nil {
		t.Fatal(err)
	}
	native, posix, _ := installDir(t)
	res := runInstall(t, nil, "--from-dir", posixPath(t, dist), "--install-dir", posix, "--no-completions")
	if res.code != 1 {
		t.Fatalf("exit %d, want 1", res.code)
	}
	if want := "installed binary reports " + impostorVersion + ", expected " + goodVersion; !strings.Contains(res.output, want) {
		t.Errorf("output lacks %q", want)
	}
	mustNotHaveTmp(t, native)
}

func TestPathWarningOnlyWhenTheDirIsAbsent(t *testing.T) {
	native, posix, canon := installDir(t)
	args := []string{"--from-dir", posixPath(t, distDir), "--install-dir", posix, "--no-completions"}

	res := runInstall(t, []string{"SHELL=/bin/bash"}, args...)
	if res.code != 0 {
		t.Fatalf("exit %d", res.code)
	}
	if !strings.Contains(res.output, canon+" is not on your PATH") {
		t.Errorf("absent dir: no PATH warning\n%s", res.output)
	}
	if !strings.Contains(res.output, `export PATH="`+canon+`:$PATH"' >> ~/.bashrc`) {
		t.Errorf("absent dir: no one-line bashrc fix\n%s", res.output)
	}
	if strings.Contains(res.output, "setx") && !strings.Contains(res.output, "Do NOT use 'setx PATH'") {
		t.Errorf("setx must only ever be mentioned as the thing not to do")
	}
	if runtime.GOOS == "windows" {
		// The Windows-side remedy names THIS directory, in Windows spelling,
		// in both the Settings sentence and the PowerShell line - never the
		// default %USERPROFILE%\.local\bin, which holds nothing here.
		win := windowsPath(t, canon)
		if !strings.Contains(res.output, "add "+win+" to your user PATH") {
			t.Errorf("absent dir: the Settings sentence does not name %s\n%s", win, res.output)
		}
		if !strings.Contains(res.output, `SetEnvironmentVariable('Path', "`+win+`;"`) {
			t.Errorf("absent dir: the PowerShell line does not name %s\n%s", win, res.output)
		}
		if strings.Contains(res.output, "USERPROFILE") {
			t.Errorf("absent custom dir: the remedy names %%USERPROFILE%%, a directory that holds nothing\n%s", res.output)
		}
	}

	// Present: the target dir prepended in the host's own PATH spelling; bash
	// converts it on startup and the script canonicalises both sides.
	withDir := "PATH=" + native + string(os.PathListSeparator) + os.Getenv("PATH")
	res = runInstall(t, []string{withDir}, args...)
	if res.code != 0 {
		t.Fatalf("exit %d with the dir on PATH\n%s", res.code, res.output)
	}
	if strings.Contains(res.output, "is not on your PATH") {
		t.Errorf("dir on PATH but the warning was printed\n%s", res.output)
	}
	if !strings.Contains(res.output, canon+" is on your PATH") {
		t.Errorf("dir on PATH but not acknowledged\n%s", res.output)
	}
}

func TestHomeWithASpaceIsTheDefaultTarget(t *testing.T) {
	// No --install-dir: the default is $HOME/.local/bin, and the owner's HOME
	// contains a space (§6 step 8). Every unquoted expansion would break here.
	home := filepath.Join(t.TempDir(), "home dir")
	if err := os.MkdirAll(home, 0o755); err != nil {
		t.Fatal(err)
	}
	posixHome := posixPath(t, home)
	canonHome := canonPath(t, home)
	env := []string{"HOME=" + posixHome}
	if runtime.GOOS == "windows" {
		env = append(env, "USERPROFILE="+home)
	}
	res := runInstall(t, env, "--from-dir", posixPath(t, distDir), "--no-completions")
	if res.code != 0 {
		t.Fatalf("exit %d, want 0\n%s", res.code, res.output)
	}
	target := filepath.Join(home, ".local", "bin", "lazyaf"+exeSuffix)
	if _, err := os.Stat(target); err != nil {
		t.Fatalf("binary not at the default target %s: %v", target, err)
	}
	if !strings.Contains(res.output, "installed "+canonHome+"/.local/bin/lazyaf"+exeSuffix) {
		t.Errorf("output does not name the space-containing target\n%s", res.output)
	}
	if runtime.GOOS == "windows" && !strings.Contains(res.output, " is on your PATH") {
		// The default target, absent from PATH: here, and only here, the
		// Windows remedy may use the %USERPROFILE% shorthand, and its two
		// spellings must both appear.
		for _, want := range []string{
			`add %USERPROFILE%\.local\bin to your user PATH`,
			`SetEnvironmentVariable('Path', "$env:USERPROFILE\.local\bin;"`,
		} {
			if !strings.Contains(res.output, want) {
				t.Errorf("default target: output lacks %q\n%s", want, res.output)
			}
		}
	}
	if got := installedVersion(t, filepath.Join(home, ".local", "bin")); got != goodVersion {
		t.Errorf("installed binary reports %q", got)
	}
	mustNotHaveTmp(t, filepath.Join(home, ".local", "bin"))
}

func TestMissingDistIsARefusalNamingTheBuild(t *testing.T) {
	_, posix, _ := installDir(t)
	res := runInstall(t, nil, "--from-dir", posixPath(t, filepath.Join(t.TempDir(), "nope")), "--install-dir", posix, "--no-completions")
	if res.code != 1 {
		t.Fatalf("exit %d, want 1", res.code)
	}
	if !strings.Contains(res.output, "is not a directory") || !strings.Contains(res.output, "bash scripts/build_cli.sh") {
		t.Errorf("refusal must name the path and the build command\n%s", res.output)
	}
}

func TestUnknownFlagIsAUsageRefusal(t *testing.T) {
	res := runInstall(t, nil, "--insecure")
	if res.code != 1 {
		t.Fatalf("exit %d, want 1", res.code)
	}
	if !strings.Contains(res.output, "unknown argument '--insecure'") {
		t.Errorf("there is no --insecure and the refusal should say so\n%s", res.output)
	}
}

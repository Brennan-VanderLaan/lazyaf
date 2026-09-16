# lazyaf

Command-line client for [LazyAF](https://github.com/Brennan-VanderLaan/lazyaf) — a
self-hosted platform for running AI coding agents as CI/CD.

One statically linked binary, no Python. It talks to a LazyAF server over HTTP:
it pushes a local repo into the server's internal git server, pulls agent-made
branches back down onto your real remote, reconciles your test suite with the
platform's test registry, drives breakpointed debug re-runs, and (from 0.3.0)
bootstraps and checks a LazyAF install with `lazyaf init` / `lazyaf doctor`.

```
your machine                        your LazyAF server
------------                        ------------------
$ lazyaf ingest ./my-app  ───────▶  internal git repo + agents
$ lazyaf branches <id>    ◀───────  branches the agents pushed
$ lazyaf land <id> -b fix ───────▶  your real origin (optionally a PR)
$ lazyaf debug attach <s> ◀──────▶  a shell in a paused pipeline's sidecar
```

> **Status.** This directory holds both the Go binary (`cmd/`, `internal/`) and,
> until the 0.3.0 cutover lands, the Python CLI it replaces (`lazyaf/`). The
> Python package is still what `pip install lazyaf-cli` (or `pip install ./cli`)
> installs today; the binary is what ships from 0.3.0 on. The plan is
> `upcoming/go-cli.md`.

## Install (from v0.3.0)

```bash
curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash
```

Installs a single static binary into `~/.local/bin` after verifying its sha256
against the release's `checksums.txt`, and tells you the one line to add if that
directory is not on your PATH. Pin a version with `LAZYAF_VERSION=v0.3.0`.
Re-running upgrades (or repairs) in place.

**Windows:** run the same line in Git Bash (it installs `lazyaf.exe` into
`%USERPROFILE%\.local\bin`), or download `lazyaf_<ver>_windows_amd64.exe` and
`checksums.txt` from the release page, verify with
`Get-FileHash -Algorithm SHA256`, and put the folder on your user PATH.

Until v0.3.0 is cut that URL 404s; build from source instead (below).

Six targets are published: linux/amd64, linux/arm64, darwin/amd64,
darwin/arm64, windows/amd64, windows/arm64 (the last is built and
version-checked but never executed in CI).

`git` must be on `PATH` for `ingest` and `land`; `lazyaf land --pr` additionally
shells out to GitHub's [`gh`](https://cli.github.com/) CLI. You also need a
running LazyAF server — see
[the quickstart](https://github.com/Brennan-VanderLaan/lazyaf/blob/main/QUICKSTART.md).

### The one place Python is still needed

The binary needs no Python. The one exception is
`lazyaf tests reconcile --from-collect`, which runs a collector *inside your
pytest* and therefore needs the interpreter your suite uses (`--python`,
`$LAZYAF_PYTHON`, `$VIRTUAL_ENV`, then `python3` / `python` / `py -3`). The
"Collecting:" line names the interpreter it chose.

## Build from source

The module lives at the repo root (`go.mod`: `github.com/Brennan-VanderLaan/lazyaf`).
Any Go ≥ 1.21 bootstraps the pinned toolchain: `go.mod` says `toolchain go1.26.8`,
so the first build on a box with an older Go downloads go1.26.8 once (~70 MB)
under the default `GOTOOLCHAIN=auto`. Nothing else is required — no CGO, no
Python, no Node.

```bash
bash scripts/build_cli.sh --host-only     # -> cli/bin/lazyaf[.exe]
cli/bin/lazyaf --version                  # lazyaf dev+<sha>[.dirty] (commit <sha>, go1.26.8, <os>/<arch>)
cli/bin/lazyaf --version --short          # dev+<sha>[.dirty]

bash scripts/build_cli.sh                 # all six targets -> cli/dist/ + checksums.txt
```

`scripts/build_cli.sh` is **the** build definition: the ldflags, the target
matrix and the checksum format live there and nowhere else. CI, the dogfood
tier and developers all call it. A plain `go build ./cli/cmd/lazyaf` also works
and reports itself as `dev+<sha>`.

### Version

The release tag is the version. `scripts/build_cli.sh` stamps it with
`-ldflags -X` on a tag build; every other build says `dev+<sha>` (and `.dirty`
when the tree has uncommitted changes), never a bare semver. Nothing in the tree
carries a CLI version to bump by hand. `lazyaf --version --short` prints only the
version token, for scripts.

### Tests

```bash
go vet ./...
go test ./cli/... -count=1 -shuffle=on
```

`cli/internal/debugproto/contract_test.go` reads `tdd/contracts/debug_terminal.v1.json`,
the debug-terminal wire contract the **server** emits
(`python scripts/gen_debug_terminal_corpus.py`), and checks the Go codec against
it byte for byte. Run the tests from a checkout; a missing corpus is a hard
failure naming the path, never a skip. The dogfood pipeline runs the same suite
as tier `TG` through `go tool gotestsum` (pinned in `go.mod`'s `tool` directive)
so `scripts/ci_gate.py` can read the junit.

House rules for the Go suite: no `t.Skip` (a test that cannot run here fails
with the reason), no test that passes without proving anything.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LAZYAF_SERVER` | `http://localhost:8000` | Server base URL. `--server/-s` on any command overrides it. |
| `NO_COLOR` | unset | When set, no ANSI colour is emitted (colour is only ever used on a terminal). |
| `LAZYAF_PYTHON` | auto | Interpreter for `tests reconcile --from-collect`. |
| `LAZYAF_COMPLETE_NO_REMOTE` | unset | When set, shell completion never asks the server for ids. |

Resolution order for the server is `--server` > `$LAZYAF_SERVER` >
`http://localhost:8000`; every message names which of the three it used. A URL
without an `http://` or `https://` scheme is refused, never guessed.

**The CLI never asks for, stores, or transmits AI provider API keys.** Anthropic
or Google credentials belong to the *server*. If you find yourself pasting a
provider key into a `lazyaf` command, stop: that is not a supported flow.

## Commands

| Command | What it does |
| --- | --- |
| `lazyaf ingest REPO_PATH --name N [-b BRANCH] [-a]` | Create a repo record and push the local repository into LazyAF's internal git server |
| `lazyaf list` | List the repos the server knows about, with their ids |
| `lazyaf branches REPO_ID` | Show the branches on the internal repo, including the ones agents created |
| `lazyaf land REPO_ID --branch B [--pr --base BASE]` | Pull an agent branch out of LazyAF and push it to your real remote; `--pr` opens a pull request via `gh` |
| `lazyaf tests reconcile REPO_ID (--refs FILE \| --from-collect [-C DIR])` | Reconcile the test registry against the **full declared test set** |
| `lazyaf debug rerun RUN_ID --break STEP` | Re-run a pipeline, pausing before the named step(s) |
| `lazyaf debug list` / `status SID` / `resume SID` / `abort SID` / `extend SID` | Drive a paused session without a TTY |
| `lazyaf debug attach SID [--token T]` | Open an interactive shell on the paused session's sidecar |
| `lazyaf init` | Create/complete `.env` with fresh secrets (never prints one) |
| `lazyaf doctor` | Check Docker, disk, ports, env and images before `docker compose up` |
| `lazyaf completion bash\|zsh\|fish\|powershell` | Print a shell completion script |

`lazyaf <command> --help` documents each; a usage error reprints the command's
own example.

### `lazyaf debug attach`

Inside the shell, **Ctrl-]** is the escape key (the same one `telnet` uses):
then `r` resumes, `a` aborts, `s` prints status, `h`/`?` prints help, `d` (or
Ctrl-D) detaches leaving the session paused, and a second Ctrl-] sends a
literal one. Every one of those verbs is also a plain `lazyaf debug`
subcommand, so controlling a session never depends on having a TTY.

The attach banner names the console mode it got:

| Mode | When | Keystrokes |
| --- | --- | --- |
| `raw-posix` | a Linux/macOS terminal | byte-exact, arrows and function keys as escape sequences |
| `raw-windows` | a Windows console with VT input | byte-exact, arrows and function keys as escape sequences |
| `raw-windows (no ANSI output)` | VT output could not be enabled on stdout | as above, but the shell's colour codes print literally |
| `line-buffered` | stdin is a pipe, or a console that cannot enter raw mode | a line at a time; full-screen programs will not render |

**Windows, stated plainly.** Raw input uses `x/term.MakeRaw`, which sets
`ENABLE_VIRTUAL_TERMINAL_INPUT` so conhost (Windows 10 1809+) and Windows
Terminal deliver arrow/Home/End/F-keys as escape sequences; window resizes are
polled every 500 ms (there is no SIGWINCH). **Whether the console read returns
per keystroke — rather than per line — under both Windows Terminal and legacy
conhost is pending the owner's manual gate step (`upcoming/go-cli.md` §12,
§13.3 #6); this was implemented non-interactively and has not yet been
exercised on a real console.** If it does not, a `ReadConsoleInput`-based reader
slots in behind the `rawInput` seam in `internal/terminal/console_windows.go`;
if neither delivers arrow keys, attach on Windows runs line-buffered and says
so. Nothing degrades silently.

## When something goes wrong

Failures follow one contract, readable by a person and by a script:

* **Diagnostics go to stderr, results go to stdout.** `lazyaf list | ...` pipes
  clean data; `2>&1` still interleaves everything for a human.
* **A failure never exits 0.** Usage errors exit 2; a command that ran and
  refused exits 1; Ctrl-C exits 130.
* **Somebody else's words are reproduced exactly.** When git or the server
  explains a failure, that explanation is quoted verbatim underneath, indented
  — including bracketed text like git's `! [rejected]`. There is no markup
  parser in the binary to eat it.
* **Errors name the remedy.** A refusal states what is wrong and the command
  that fixes it, rather than pointing at `--help`.
* **The backend is always named.** Connection failures print the URL in use and
  where it came from (`--server`, `$LAZYAF_SERVER`, or the built-in default),
  and a timeout is told apart from a refused connection.

```console
$ lazyaf list --server localhost:8000
Error: the LazyAF backend URL has no http:// or https:// scheme: localhost:8000

Read from --server. The scheme is required rather than guessed - guessing http:// is how a request that should have been encrypted goes out in the clear.

    LAZYAF_SERVER=http://localhost:8000
    lazyaf list --server http://localhost:8000
```

The CLI is non-interactive by design: it never prompts, so it behaves the same
under a harness as it does in a terminal.

## Layout

```
cli/cmd/lazyaf/main.go        os.Exit(cmd.Execute()) and nothing else
cli/internal/cmd/             the cobra command tree
cli/internal/api/             THE one HTTP client; server URL resolution; typed responses
cli/internal/ui/              stderr/stdout discipline, exit codes, the ANSI helper
cli/internal/debugproto/      the debug-terminal codec, pinned to the server's by the corpus
cli/internal/terminal/        the attach client: driver, websocket dialer, consoles
cli/internal/version/         Version / Commit, set by -ldflags -X
cli/internal/gitx, reconcile, envfile, initcmd, doctor   the remaining commands
scripts/build_cli.sh          the one build definition
tdd/contracts/                the wire contract as data (written by the server's generator)
```

## License

MIT — see [LICENSE](https://github.com/Brennan-VanderLaan/lazyaf/blob/main/LICENSE).

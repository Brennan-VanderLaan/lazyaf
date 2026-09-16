# The Go CLI — Implementation Plan (0.3.0)

**Status**: ready to brief · **Owner decisions recorded**: hard cutover; 0.3.0; `init`/`doctor` fold in; `build_images.py` stays Python; install into `~/.local/bin` · **Standing rules**: R1 nothing dark · R2 delete only after acceptance · R3 one source of truth per wire contract · R4 no fake green · R5 async‑first · R6 real seams · R7 dogfood ratchet · R8 every Go command ships tests

Three designs were judged. OPERATOR‑FIRST won (269) over CONTRACT‑FIRST (256) and DISTRIBUTION‑FIRST (224). This plan is OPERATOR‑FIRST's spine with the judges' named grafts from the other two. Where the three disagreed, the decision and its reason are stated inline; nothing below is left as "either". Every file:line was re‑verified against `main` at `5a91734` on 2026‑09‑16 before it was written down; where a fact could not be verified from this box it is marked **UNVERIFIED** and assigned to the phase that verifies it.

---

## 1. The decision, in one page

### 1.1 What ships

One statically linked Go binary, `lazyaf`, built from `cli/cmd/lazyaf` in a **root** `go.mod` (`module github.com/Brennan-VanderLaan/lazyaf`), released as **0.3.0** through the release‑please flow that already exists. It carries every command the Python CLI has today (`ingest`, `land`, `list`, `branches`, `tests reconcile`, `debug rerun|list|status|attach|resume|abort|extend`), plus three new things the owner asked for or that the tree already promised: `lazyaf init` (was `scripts/bootstrap_secrets.py`), `lazyaf doctor` (was `scripts/preflight.py`), `lazyaf completion bash|zsh|fish|powershell`, and `--token` on `debug attach` so the `join_command` the server already prints (`backend/app/routers/debug.py:119-121`) stops being a lie. Six targets: linux/amd64, linux/arm64, darwin/amd64, darwin/arm64, windows/amd64, windows/arm64. Installed by `curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash` into `~/.local/bin`, with sha256 verification, completion, and a PATH warning that never edits an rc file.

### 1.2 What is deleted — in the acceptance commit and not before (R2)

`cli/lazyaf/` (2546 lines of click + httpx + rich), `cli/pyproject.toml`, `cli/build/`, `cli/lazyaf_cli.egg-info/`, `cli/uv.lock` (untracked today); `scripts/bootstrap_secrets.py`, `scripts/preflight.py`; `tdd/unit/packaging/` (whole directory); `tdd/unit/scripts/test_cli_debug.py`, `test_cli_errors.py`, `test_cli_tests_reconcile.py`, `test_bootstrap_secrets.py`, `test_preflight.py`; the cross‑import half of `tdd/unit/debug/test_terminal_protocol_contract.py`; the `wheel` and `pypi` jobs of `release.yml` and the `cli-wheel` job of `pr-build.yml`; `.github/scripts/check_release_version.py` (replaced, see §4); the `extra-files` block of `release-please-config.json`; `rich` from `backend/pyproject.toml`'s `test` extra (`:46`, added only for `TestAgainstRealRich`). The deletion is refused by CI until the parity ledger (§11) says every one of those tests went somewhere.

### 1.3 What stays Python, and why

- **The pytest collector plugin** (`lazyaf_collect_plugin.py`, today a string literal at `cli/lazyaf/cli.py:996-1045`). `lazyaf tests reconcile --from-collect` runs it *inside the user's pytest* — it is Python because pytest is. The Go binary embeds it (`//go:embed`) and orchestrates; the interpreter it picks is named in the output. Every other command needs no Python. This is the one place the "no Python locally" goal is bounded, and `--help` says so.
- **`scripts/build_images.py`** — the docker‑SDK build tool and the single source of the images table CI reads. Out of scope by owner decision. The Go doctor gets the step‑image list from a *generated* Go file (§8) so that table stays the only source.
- **`scripts/run_tier.py`, `scripts/ci_gate.py`, `scripts/verify_executor.py`** — they run inside the tier image where Python is guaranteed, and they are the single source of tier selection and gating. They gain a Go tier; they are not rewritten.

### 1.4 Explicitly out of scope

An `install.ps1`; a Homebrew tap, scoop bucket or winget manifest (all need a PAT — the CI's only credential is `GITHUB_TOKEN`, `.github/WORKFLOWS.md` security posture); goreleaser; cosign/attestations; a `self-update` verb; a JSON output mode; a colour/markup library; completion of `--break` step keys; `-race` in the tier (needs CGO; the image is `CGO_ENABLED=0`); a backend version handshake (`backend/app/main.py:159` still says `version="0.1.0"` while v0.2.0 shipped — a real drift, recorded here as a follow‑up, not done in this wave); a fix for the T1 flakes named in `tier_floors.json`.

---

## 2. Module layout, dependencies, build flags

### 2.1 Where `go.mod` lives — DECIDED: the repo root

`module github.com/Brennan-VanderLaan/lazyaf`, `go 1.26.0`, `toolchain go1.26.8`, at the repo root. OPERATOR‑FIRST put it under `cli/`; the other two put it at the root. Root wins for one reason that outweighs the tidiness argument: `install.sh`'s fallback for an unsupported platform is `go install github.com/Brennan-VanderLaan/lazyaf/cli/cmd/lazyaf@v0.3.0`, and that only resolves against the plain `vX.Y.Z` tags release‑please produces (`include-component-in-tag: false`, `.github/release-please-config.json`). A module under `cli/` needs `cli/v0.3.0` tags that nothing creates, so the fallback would be a lie (R1). Cost, stated: the module zip on proxy.golang.org is the whole tagged tree (tracked files only; `frontend/dist`, `.venv`, `node_modules` are gitignored — verified `.gitignore`) — well under the proxy's 500 MB limit; `go vet ./...` and `go test ./...` from the root walk only directories containing `.go` files, and there are zero `.go` files anywhere in the tree today (verified with `find`), so both touch only `cli/`.

`cli/` **must keep existing**: `tdd/unit/services/test_no_legacy_code.py:165-200` lists `"cli"` in `SEARCH_ROOTS` and `test_search_roots_all_exist` (`:260-268`) fails if a root vanishes. The Go tree lives under `cli/`, so the deletion and the creation land in one directory and the grep gate keeps pointing at the shipping CLI. `SEARCHABLE_SUFFIXES` (`:224-227`) gains `.go` in the deletion commit.

### 2.2 Go version — DECIDED: `go 1.26.0` directive, `toolchain go1.26.8`, `GOTOOLCHAIN=local` in the image

`golang.org/x/term` v0.46.0 declares `go 1.26.0` (proxy `.mod`, verified by two judges), so the directive cannot be lower. go1.26.8's linux‑amd64 sha256 `d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b` and linux‑arm64 `211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0` were verified by two judges against go.dev/dl; **L5 re‑verifies both from `https://go.dev/dl/?mode=json` before the Dockerfile commit**. The dev box has go1.21.5 with `GOTOOLCHAIN=auto` (verified: `go env GOTOOLCHAIN` → `auto`), so the first `go build` downloads go1.26.8 once (~70 MB) — `cli/README.md` says so. The test‑runner image bakes exactly go1.26.8 with `GOTOOLCHAIN=local`, so a future `go.mod` bump beyond the image fails with `go: go.mod requires go >= X (running Y; GOTOOLCHAIN=local)` instead of downloading mid‑tier (R1), and a T1 test pins `ARG GO_VERSION` in the Dockerfile to the `toolchain` line (§10.1). DISTRIBUTION‑FIRST's `go 1.21` directive is rejected: it cannot hold `x/term` v0.46.0 or `coder/websocket` v1.8.15 (`go 1.23`), and `go mod tidy` would raise it on first run.

### 2.3 Tree

```
go.mod  go.sum                          root; the ONLY module
cli/
  cmd/lazyaf/main.go                    package main: os.Exit(cmd.Execute()); nothing else
  internal/cmd/                         cobra tree. root.go (persistent --server/-s, $LAZYAF_SERVER, --version),
                                        ingest.go land.go list.go branches.go tests.go tests_reconcile.go
                                        debug.go debug_rerun.go debug_list.go debug_status.go debug_attach.go
                                        debug_resume.go debug_abort.go debug_extend.go init.go doctor.go completion.go
  internal/api/                         THE one HTTP client (cli.py:250-327 idiom), ResolveServerURL / DescribeServer
                                        (cli.py:153-215), typed response structs (schemas/{repo,debug,testref}.py)
  internal/ui/                          Fail / Warn / Verbatim / Remedy on stderr, results on stdout, exit codes;
                                        NO markup parser; 15-line ANSI helper gated on IsTerminal and NO_COLOR
  internal/gitx/                        git / gh via os/exec behind a Runner interface (the seam TestLand needs)
  internal/debugproto/                  constants.go frame.go escape.go close.go; contract_test.go reads
                                        ../../../tdd/contracts/debug_terminal.v1.json
  internal/terminal/                    url.go run_terminal.go (RunTerminal over Socket + ConsoleIO interfaces)
                                        dial.go (coder/websocket adapter) console_posix.go console_windows.go console_pipe.go
  internal/reconcile/                   manifest.go collect.go lazyaf_collect_plugin.py (//go:embed)
  internal/envfile/                     .env model shared by init AND doctor: parse / plan / lock / atomic write;
                                        Secret type; placeholders_gen.go (GENERATED, see §8.3)
  internal/initcmd/                     `lazyaf init`
  internal/doctor/                      `lazyaf doctor`; step_images_gen.go (GENERATED, see §8.3)
  internal/version/                     var Version = "dev"; Describe()
  internal/parity/                      ledger_test.go (Enforcement B, §11)
  internal/installsh/                   install_test.go: runs ../../install.sh against a local dist (TG)
  install.sh                            committed AND attached to every release
  README.md                             rewritten for the binary
  bin/  dist/                           gitignored build outputs
scripts/build_cli.sh                    THE ONE build definition (ldflags, targets, checksums)
scripts/gen_debug_terminal_corpus.py    writes tdd/contracts/debug_terminal.v1.json (§3)
scripts/gen_cli_contracts.py            writes the two *_gen.go files (§8.3)
tdd/contracts/debug_terminal.v1.json    the wire contract as data
tdd/contracts/cli_parity.json           the parity ledger (§11)
```

Embed rule: `//go:embed` cannot reach outside the package directory (no `..`). That is why the plugin `.py` sits inside `internal/reconcile`, why the two generated Go files sit inside their consuming packages, and why the codec corpus is **not** embedded — it is read from `tdd/contracts` at test time only (the shipped binary never needs it), and a missing corpus is `t.Fatalf` with the absolute path and "run from a LazyAF checkout", never `t.Skip` (R4).

### 2.4 Dependencies (module@version, each justified)

| Module | Version | Why it earns its weight |
|---|---|---|
| `github.com/spf13/cobra` | v1.10.2 (`go 1.15`; resolves from this box) | Subcommand tree, `Example:` per command reprinted on usage errors (port of `LazyafCommand.parse_args`, `cli.py:389-410`), Levenshtein "did you mean" (port of `LazyafGroup.resolve_command`, `:420-428`), **and** the bash/zsh/fish/powershell generators plus the hidden `__complete` protocol that makes dynamic completion possible. Four shell dialects by hand is ~1500 lines of shell cobra already maintains. Pulls `spf13/pflag` v1.0.10 and `inconshreveable/mousetrap` (Windows double‑click guard). |
| `github.com/coder/websocket` | v1.8.15 (`go 1.23`; zero transitive deps) | WebSocket client for `debug attach`. `HTTPHeader` on Dial for `Authorization: Bearer <token>` (the token never enters the URL — `debug_cmd.py:71-88`, pinned by `test_cli_debug.py:231`); `CloseError{Code, Reason}` so a pre‑accept 4401's sentence survives; the `*http.Response` on a rejected handshake so the 403‑no‑reason path (`debug_cmd.py:139-153`) ports without guessing. Over gorilla v1.5.3 (no context API) and nhooyr (the same code before its rename). **Its default read limit is 32 KiB, below `MAX_FRAME_BYTES` = 65536; `dial.go` calls `SetReadLimit(debugproto.MaxFrameBytes)` and `TestReadLimitIsTheContractBound` pins it** (DISTRIBUTION‑FIRST's catch; a 64 KiB stdout frame would otherwise close 1009). |
| `golang.org/x/term` | v0.46.0 (`go 1.26.0`) | `MakeRaw`/`Restore`/`GetSize`/`IsTerminal` with a real Windows implementation that sets `ENABLE_VIRTUAL_TERMINAL_INPUT`, so arrow/function keys arrive as ESC sequences and the msvcrt translation table (`debug_cmd.py:313-360`) is deleted, not ported. |
| `golang.org/x/sys` | v0.48.0 (transitive of x/term) | Used directly for exactly three things: `windows.SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)` on stdout (port of `debug_cmd.py:281-297`), `unix.Statfs` / `windows.GetDiskFreeSpaceEx` for doctor's disk check, and a raw `unix.Socket`/`Bind` **without `SO_REUSEADDR`** for doctor's port probe (Go's `net.Listen` sets it on Linux and would report the false "free" `preflight.py:428-429` warns about). |
| `gotest.tools/gotestsum` | v1.13.0 (`go 1.24.0`) | **Test tool only**, declared with the `go.mod` `tool` directive and run as `go tool gotestsum --junitfile`. Exists because `scripts/ci_gate.py:50-66` reads junit `<testcase>` elements and `go test` emits none. Pinned in one file (`go.mod`), bumped by dependabot with everything else. Not linked into the binary. |

Deliberately absent: viper (one env var is not a config system), testify, any TUI/colour library (the rich bug class is why the CLI has 55 error tests; plain writes remove the hazard), a YAML library (doctor reads compose `image:` lines by regex, the depth `preflight.py:656-669` reads at), creack/pty, go‑isatty.

### 2.5 Build flags — one place, `scripts/build_cli.sh`

```
CGO_ENABLED=0 go build -trimpath \
  -ldflags "-s -w -X github.com/Brennan-VanderLaan/lazyaf/cli/internal/version.Version=${V} \
               -X github.com/Brennan-VanderLaan/lazyaf/cli/internal/version.Commit=${SHA}" \
  -o cli/dist/lazyaf_${V}_${GOOS}_${GOARCH}${EXT} ./cli/cmd/lazyaf
```

for the six `GOOS/GOARCH` pairs, then `sha256sum lazyaf_* > cli/dist/checksums.txt`. `V` is `${GITHUB_REF_NAME#v}` when `GITHUB_REF_TYPE=tag`, else `dev` (in which case `-buildvcs` is left ON so `ReadBuildInfo` carries the sha — see §4). `SHA` is `GITHUB_SHA` or `git rev-parse HEAD`. `--host-only` builds only the host target to `cli/bin/lazyaf[.exe]`. Bash, runs under Git Bash on the owner's box. `release.yml`, `pr-build.yml`, the TG tier preflight and developers all call this script, so the ldflags live in one file (R3).

---

## 3. THE CODEC CONTRACT (R3)

### 3.1 The problem exactly

`cli/lazyaf/debug_protocol.py` is a byte‑exact mirror of `backend/app/services/execution/debug_terminal.py:53-103`, allowed to exist twice only because `tdd/unit/debug/test_terminal_protocol_contract.py` imports **both** (`:32-33`) and (1) compares 22 scalars + 2 sets by name (`:41-66`) plus a *derived* set of every `TYPE_*` attribute (`:86-100`); (2) round‑trips every frame type both ways with byte‑exact payloads including non‑UTF‑8 (`:110-194`) and asserts one client→server frame is byte‑identical from both encoders (`:182-194`); (3) refuses 16 malformed frames on both sides (`:199-235`); (4) base64 parity (`:256-277`); (5) escape keys map onto `COMMANDS` (`:280-301`). None of it can import Go. Without a replacement, the Go codec is a third copy pinned by nothing.

### 3.2 The replacement: the server EMITS the contract as data; both sides are tested against it; T1 proves it is fresh

R3 is satisfied because only the server codec can *write* the corpus; Go can only be tested against it.

**Who emits.** `backend/app/services/execution/debug_terminal.py` gains `export_contract() -> dict`, a pure function over the module's own constants and encoders (the module already imports nothing from docker/fastapi at import time, `:18-22`, which is why the CLI test could import it; the exporter keeps that). `scripts/gen_debug_terminal_corpus.py` (stdlib + that import, using the `sys.path` trick at `test_terminal_protocol_contract.py:29`) writes `json.dumps(sort_keys=True, indent=2) + "\n"` to `tdd/contracts/debug_terminal.v1.json`; `--check` exits 1 with a unified diff. Its docstring states it is the ONLY writer and that a hand edit is a bug. `.v1` is `PROTOCOL_VERSION`; a v2 is a new file both consumers opt into.

**The corpus, section by section.**

`constants.scalars` — exactly the 22 names of `SHARED_SCALARS`, with the values the server holds today:

| Name | Value | Name | Value |
|---|---|---|---|
| `PROTOCOL_VERSION` | `1` | `CLOSE_NORMAL` | `1000` |
| `TYPE_STDIN` | `"stdin"` | `CLOSE_BAD_TOKEN` | `4401` |
| `TYPE_RESIZE` | `"resize"` | `CLOSE_NOT_ATTACHABLE` | `4403` |
| `TYPE_COMMAND` | `"command"` | `CLOSE_UNKNOWN_SESSION` | `4404` |
| `TYPE_PING` | `"ping"` | `CLOSE_DUPLICATE_TERMINAL` | `4004` |
| `TYPE_READY` | `"ready"` | `CLOSE_BOUND_EXCEEDED` | `4009` |
| `TYPE_STDOUT` | `"stdout"` | `MAX_FRAME_BYTES` | `65536` |
| `TYPE_NOTICE` | `"notice"` | `MAX_OUTBOUND_QUEUE` | `256` |
| `TYPE_CLOSED` | `"closed"` | `RATE_WINDOW_SECONDS` | `1.0` |
| `TYPE_PONG` | `"pong"` | `RATE_MAX_FRAMES_PER_WINDOW` | `200` |
| `COMMANDS` | `["@resume","@abort","@status","@help"]` (an ARRAY — order matters, `:102-104`) | `CONNECTION_MODE_SIDECAR` | `"sidecar"` |

`constants.sets` — the 2 sets, as sorted lists: `CLIENT_FRAME_TYPES = ["command","ping","resize","stdin"]`, `SERVER_FRAME_TYPES = ["closed","notice","pong","ready","stdout"]`.

`constants.derived_types` — every module attribute named `TYPE_*` (the `:86-100` mechanism), so a frame type added to the server without regenerating changes this list.

`constants.reasons` — `SHELL_REFUSED_REASON` and `REMOTE_ATTACH_REASON` (`debug_terminal.py:94-103`). `test_cli_debug.py:560` pins the CLI's `--shell` refusal to the server's sentence word for word; that pin must survive the port, and it can only survive if the Go test reads the sentence from data the server wrote.

`frames` — a list of cases `{"name", "direction": "server->client"|"client->server", "type", "fields": [[key, value], ...], "data_hex"?, "wire"}`. `fields` is an **ordered array of pairs**, not an object: Go decodes JSON objects into unordered maps, and the byte‑identical assertion needs the key order Python's `json.dumps` used. `wire` is the exact string `debug_terminal.encode_frame` produced. Cases: `ready` (mode=sidecar, container_id=`abc123def456`); `stdout` of `bytes(range(256))`; `stdout` of `b"\xff\xfe\x00\x80 not utf-8 \x9c"`; `notice("/workspace is rw")`; `closed("resumed")`; `closed` with `reason=None` (proves None‑dropping: wire is `{"v":1,"type":"closed"}`); `pong`; `stdin` of `bytes(range(256))`; `stdin` of `b"ls -la /workspace\n"`; `resize` 120×40; `ping`; `command` ×4, one per verb from `constants.scalars.COMMANDS`.

`malformed` — the 16 `MALFORMED` entries (`:199-216`) verbatim as `{"name","raw"}`:
`not-json`, `not-an-object`, `wrong-version`, `missing-version`, `unknown-type`, `stdin-without-data`, `stdin-non-string-data`, `stdin-not-base64`, `stdout-not-base64`, `resize-missing-rows`, `resize-zero`, `resize-negative`, `resize-bool`, `resize-float`, `unknown-command`, `command-without-verb`; plus `{"name":"not-utf8","raw_hex":"fffe7b7d"}` (`:231`); plus two encoder cases `{"encode_unknown_type":"exec"}` and `{"encode_raw_bytes_in_data":true}` (`:236-247`).

`base64` — `encode: [{"bytes_hex","text"}]` for the six vectors at `:257-262` (`""`, `"a"`, `"\x00"`, `bytes(range(256))`, `"\xff"*1024`, `"héllo"` utf‑8), `decode` for the three at `:266-268`, and `reject: ["A"]` (`:271-277`).

### 3.3 The Python side (T1) — `test_terminal_protocol_contract.py` REWRITTEN, not deleted

- `TestCorpusIsCurrent::test_committed_file_equals_a_fresh_export` — regenerate in memory, byte‑compare with the committed file; failure message: `corpus stale - run python scripts/gen_debug_terminal_corpus.py and commit`. **This is what fails when a server constant moves and nobody regenerated.**
- `TestServerSatisfiesItsOwnCorpus` — the server's encoder reproduces every `frames[].wire`; its decoder accepts every frame and yields the listed fields and bytes; it refuses every `malformed` case with `DebugProtocolError`; base64 vectors agree. So a hand‑edited corpus the server itself cannot satisfy is caught even before the generator runs (an exporter that lies cannot produce a green file).
- `TestDerivedTypes::test_every_TYPE_star_is_in_the_corpus` — the `:86-100` check against `constants.derived_types`.
- `TestEscapeSurfaceIsClientOnly::test_the_server_has_no_escape_byte` — `not hasattr(server, "ESCAPE_BYTE")` (`:284`).
- **Until the deletion commit**, the file ALSO keeps every cross‑import class against the Python client (`TestConstantParity`, `TestServerToClient`, `TestClientToServer`, `TestRejectionParity`, `TestBase64Parity`, `TestClientOnlySurface`). If the Python client passes the corpus, the corpus pins what the wire actually is today. The deletion commit drops those classes and the `from lazyaf import debug_protocol` import.

### 3.4 The Go side (TG) — `cli/internal/debugproto/contract_test.go`

- `TestContractConstants` — a table from JSON key → Go value for every scalar and set; asserts equality **and** that the JSON has no key the table does not cover (`unknown contract constant X - add it to debugproto`), so a new server constant fails Go rather than being ignored. `debugproto.Commands` (a `[]string`) must equal `COMMANDS` in order. `debugproto.ShellRefusedReason` and `RemoteAttachReason` equal `constants.reasons`.
- `TestContractFrames` — one `t.Run` per case (so gotestsum counts each, and a corpus that silently shrinks lowers TG's executed count into its floor — R4 for free): `Encode(type, orderedFields)` must equal `wire` **byte‑exact** for every case in both directions (Go's encoder is generic, so re‑encoding the server frames is a free extra pin); `Decode(wire)` must yield the fields; `DecodeBytes(data) == data_hex`. Go therefore encodes with an ordered writer, not a map: `"v"` first, `"type"` second, fields in the given order, compact separators, `json.Encoder.SetEscapeHTML(false)` (Python's `json.dumps` does not escape `<`; the emitted fields are base64, ints and `@verbs`, so `ensure_ascii` cannot bite either).
- `TestContractMalformed` — every case → `errors.As(err, *ProtocolError)`; the two encoder cases refuse. Go has no `ValueError` hierarchy; the `:250-253` property becomes "every refusal is a `*ProtocolError` and nothing else is" (stated deviation). `DecodeBytes` uses `base64.StdEncoding.Strict()` so `"A"`, `"not!base64"` and `"@@@"` fail like `validate=True`; the not‑UTF‑8 raw frame is refused via `utf8.Valid` before JSON.
- `TestContractBase64` — the encode/decode/reject vectors.
- `TestEscapeKeysMapToWireCommands` — `set(EscapeKeys values) == set(constants.scalars.COMMANDS)` and every verb is reachable from a key (`:292-301`). `EscapeByte == 0x1d`; keys `r a s h ?` → `@resume @abort @status @help @help`; detach keys `d` and `0x04` (`debug_protocol.py:188-200`).
- Corpus missing → `t.Fatalf` with the absolute path; never `t.Skip`.

### 3.5 What fails when something moves

| Change | First red | Then |
|---|---|---|
| Server changes a constant / verb order / close code / bound, no regen | T1 `test_committed_file_equals_a_fresh_export` | dev regenerates → T1 green, TG `TestContractConstants` red until Go follows |
| Go changes a constant alone | TG | the corpus cannot be regenerated from Go: the generator imports the server, by design |
| Someone hand‑edits the JSON to match Go | T1 stale‑vs‑regeneration, usually also `TestServerSatisfiesItsOwnCorpus` | — |
| A key order / separator / whitespace change in either encoder | `TestContractFrames` byte compare (Go) or `TestServerSatisfiesItsOwnCorpus` (Python) | — |
| A new frame type on the server | `derived_types` changes → T1 red until regen → TG red until the Go table gains it | — |
| A new validation rule on the server | the exporter's malformed table is `MALFORMED` itself, which moves into the module; T1 red until regen → TG red until Go refuses it too | — |

Bounds are **derived, not restated**: the Go client caps a stdin read at `(MaxFrameBytes − envelope) × 3/4` raw bytes (48 KiB) so no frame it sends can exceed `MAX_FRAME_BYTES`; the cap is computed from the constant the corpus checks. The corpus pins encode/decode and constants, not the server's runtime enforcement of rate/queue/size — that is the same coverage the cross‑import test has today, and T3's live run is the only cross‑check (§15).

### 3.6 The live meeting point survives, on the binary

`tdd/e2e/test_debug_rerun.py` (T3, floor 21 / measured 22) is "the only place the CLI's codec and the server's codec meet over a socket" (tier_floors T3 note). Today it imports `lazyaf.debug_cmd` (`:61`, verified) and drives `run_terminal` over a real uvicorn socket. **At P2 — before anything is deleted — it switches to driving the built binary as a subprocess**: `lazyaf debug attach <sid> --token <t> --server <url>` with stdin piped (the client is in line mode, escape decoder still applied to raw bytes), feeding `touch <file>\n` then `\x1dr`, asserting the run completes, the exit code is 0 and the READ‑WRITE banner is on stderr. Binary path from `$LAZYAF_CLI_BIN`, default `<repo>/cli/bin/lazyaf[.exe]` (left there by the TG preflight, §10.4); absent → `pytest.fail` naming `bash scripts/build_cli.sh --host-only`, never skip. DISTRIBUTION‑FIRST deferred this switch to after 0.3.0, which would orphan the test at cutover with a floor margin of one; that ordering is rejected.

---

## 4. Version: one source of truth

### 4.1 Decision — the tag is the version; the binary learns it through `-ldflags -X`; nothing in the tree carries a bumpable CLI version

release‑please already keeps the manifest (`.github/.release-please-manifest.json`, `{".": "0.2.0"}`) and derives the tag from it (`release-please.yml`, `WORKFLOWS.md:404-460`). The `extra-files` entry (`release-please-config.json`, `packages["."].extra-files` → `cli/lazyaf/__init__.py:19-21`) exists only because a wheel must carry its version in the package. That reason dies with the wheel. A `version.go` bumped by `extra-files` (DISTRIBUTION‑FIRST) would keep a second rendered copy AND make a source build at any commit after the release PR merged report `0.3.0` while being N commits past it — the "dev must say dev" violation. Its "ldflags is a tautology" objection is answered by checking the *recorded* ldflags of all six cross‑compiled binaries (§4.4), which proves every sibling carries the tag without executing it.

### 4.2 Mechanics

`cli/internal/version/version.go`:

```go
package version

// Set by scripts/build_cli.sh via -ldflags -X. Left at "dev" by a plain `go build`.
var Version = "dev"
var Commit = ""

// Describe never returns a bare semver unless Version was set by ldflags.
func Describe() string  // "0.3.0" | "dev+5a91734" | "dev+5a91734.dirty" | "dev+unknown"
func Line() string      // "lazyaf 0.3.0 (commit 5a91734, go1.26.8, linux/amd64)"
```

For a dev build, `Commit`/dirty come from `runtime/debug.ReadBuildInfo()` `vcs.revision`/`vcs.modified` (stamped by any `go build` from a git checkout since Go 1.18; a tarball build says `dev+unknown`). Release builds pass `-buildvcs=false` and set `Commit` explicitly so the bytes are identical wherever the tag is built.

### 4.3 What `lazyaf --version` prints

- Tag build: `lazyaf 0.3.0 (commit 5a91734, go1.26.8, linux/amd64)`
- Dev build: `lazyaf dev+5a91734.dirty (commit 5a91734, go1.26.8, windows/amd64)`
- `lazyaf --version --short`: `0.3.0` / `dev+5a91734.dirty` (for scripts and the CI check)

The second whitespace‑separated token of the long line is the version. `version.TestDevBuildSaysDev` (Version unset → `Describe()` starts with `dev+`), `version.TestDescribeNeverBareSemverWhenDev`, `version.TestLineIsParseable`, and `cmd.TestVersionNeverDials` (runs `--version` with `LAZYAF_SERVER` pointed at a closed port and a dial recorder; asserts zero dials).

### 4.4 CI proves binary == tag

`.github/scripts/check_binary_version.py` (new, stdlib) replaces `check_release_version.py` (deleted): `--tag v0.3.0 --dist cli/dist` runs `go version -m <binary>` on **all six** binaries — Go prints build settings for any GOOS/GOARCH binary without executing it — parses the `build -ldflags=` line for `internal/version.Version=<v>`, and refuses (exit 1) any binary whose value != `${TAG#v}`; exit 2 if `dist/` holds no binaries. Exact string compare; the PEP 440 normalisation at `check_release_version.py:41-42` was a wheel problem. `tdd/unit/scripts/test_check_binary_version.py` (T1) feeds it canned `go version -m` transcripts: six matching → 0; one mismatched → 1 naming the binary; a binary with no ldflags line → 1; empty dist → 2. The release job additionally executes the host binary's `--version --short` and compares it to the tag (bytes run, not just bytes recorded). Both are hard stops before `gh release`.

#### 4.4.1 Corrections recorded at integration (2026-09-16, verified on go1.26.8)

Two facts §2.5 and §4.4 had wrong, found by the release lane and confirmed by the integrator:

- **`-buildvcs` stays ON for tag builds too.** Under `-trimpath`, go1.26.8 records **no** `build -ldflags=` line in the binary at all (cmd/go omits it deliberately), so "parse the ldflags line" cannot work. `scripts/build_cli.sh` therefore keeps VCS stamping on for every build, and `.github/scripts/check_binary_version.py` validates the **VCS-stamped main-module version** (`go version -m` → `mod … v0.3.0`) and `vcs.modified=false` against the tag, on all six binaries — which is a stronger claim than the ldflags string (it proves the bytes came from a clean checkout *at* the tag). Consequence: a positive check is only possible at a real tag; on an untagged or dirty checkout the tool refuses naming the pseudo-version, which is the R4 shape wanted. The integrator proved the negative locally (v9.9.9 dist vs tag → exit 1, and a missing-matrix dist → exit 1) and the positive with a local, never-pushed tag on a clean tree.
- **Gate #1c's grep** must not be anchored; `go test -v` indents subtest result lines.

### 4.5 release‑please wiring, verbatim

In `.github/release-please-config.json`, delete the whole `"extra-files": [ ... ]` array from `packages["."]` so the package block reads:

```json
  "packages": {
    ".": {
      "package-name": "lazyaf",
      "changelog-path": "CHANGELOG.md"
    }
  },
```

Nothing else moves: `release-type: simple`, `include-v-in-tag: true`, `include-component-in-tag: false`, `bump-minor-pre-major: true`, `separate-pull-requests: true`, `bootstrap-sha` unchanged. **This edit travels in the same commit as the deletion of `cli/lazyaf/__init__.py` (P4).** release‑please v17's behaviour on a configured generic extra‑file whose path no longer exists is **UNVERIFIED** (it cannot be run locally — `WORKFLOWS.md:605` records the same limit); landing both in one commit means the worst case is a visibly failed release‑please run on the next push to main, not a mis‑bumped file. P4's checklist re‑dispatches `release-please.yml` and inspects the refreshed release PR: its diff must be `CHANGELOG.md` + the manifest and nothing else.

Bump semantics unchanged (`CONTRIBUTING.md:34-43, :84-106`): pre‑1.0 `feat` → minor, `feat!` → minor. **Every commit from P0 to P3 uses a hidden type** (`build(cli):`, `test:`, `ci:`, `chore:`) so the standing release PR does not become a `feat` release that still ships a wheel; the P4 cutover commit is `feat(cli)!: replace the Python CLI with a Go binary; lazyaf init / lazyaf doctor replace the onboarding scripts` and makes the release PR read 0.3.0. **The release PR is not merged until P4 has landed** — release‑please refreshes it on every push and it becomes mergeable long before the cutover; this is written into `WORKFLOWS.md`'s pre‑merge checklist (`:587-590` becomes "the release PR diff is CHANGELOG.md and the manifest, nothing else; the binary's version is the tag, proven by `check_binary_version.py`"). `CONTRIBUTING.md:124-143` ("the version number lives in exactly one place") is rewritten to say: the tag, via release‑please's manifest; nothing in the tree to bump by hand.

---

## 5. The command surface

Global: `--server/-s URL` is a **persistent** flag on the root (the Python CLI repeats it per command; every subcommand still accepts it in the same position, so scripts do not change). Resolution: flag > `$LAZYAF_SERVER` > `http://localhost:8000`; every message names which of the three it used (`describe_server`, `cli.py:153-176`); a schemeless or empty URL is refused, not guessed (`:176-215`); trailing slash stripped. `--version` on the root. Output is plain text; refusals and warnings on stderr, results on stdout; exit 0 ok / 1 failure / 2 usage / 130 interrupt. Every command has a cobra `Example:` and a usage error reprints it; a mistyped subcommand gets cobra's suggestion (`lazyaf lst` → `list`); a wholly unknown one gets none.

| Go command | Python twin (`cli/lazyaf/cli.py`) | Options (identical unless noted) | Output changes |
|---|---|---|---|
| `ingest REPO_PATH` | `ingest` `:500-505` | `--name/-n` (required) `--branch/-b` `--all-branches/-a` | none; refuses blank name, nonexistent branch (lists the real ones), not‑a‑repo, no commits — before any HTTP |
| `land REPO_ID` | `land` `:669-675` | `--branch/-b` (required) `--remote/-r` (default `origin`) `--pr` `--base` | none; push refspec `refs/remotes/lazyaf/<b>:refs/heads/<b>` (`:~767`); `gh pr create --base --head --fill` (`:783-784`) |
| `list` | `list_repos` `:816-817` | — | none |
| `branches REPO_ID` | `branches` `:845-847` | — | none |
| `tests reconcile REPO_ID [PYTEST_ARGS...]` | `reconcile` `:1139-1178` | `--refs/--manifest/-m PATH` `--from-collect` `--collect-path/-C DIR` `--allow-results-manifest`; **new** `--python PATH` | the "Collecting:" line names the interpreter chosen (`--python` > `$LAZYAF_PYTHON` > `$VIRTUAL_ENV` > `python3` > `python` > `py -3` on Windows); none found → refusal naming all of them |
| `debug rerun RUN_ID` | `debug_rerun` `:1335-1358` | `--break STEP_KEY` (repeatable) `--commit` `--branch` `--timeout SECS` | none |
| `debug list` | `debug_list` `:1396-1397` | — | none |
| `debug status SID` | `debug_status` `:1442-1444` | — | none |
| `debug attach SID` | `attach` `debug_cmd.py:521-536` | `--sidecar/--shell` (default sidecar) `--print-credential`; **new** `--token T` | with `--token` the client skips `POST join-token` and the server's `join_command` (`debug.py:119-121`) is now literally true; attach banner names the console mode (`raw`, `raw-windows`, `raw-windows (no ANSI output)`, `line-buffered`) |
| `debug resume SID` | `debug_resume` `:1515-1523` | `--all` | none |
| `debug abort SID` | `debug_abort` `:1544-1546` | — | none |
| `debug extend SID` | `debug_extend` `:1560-1563` | `--minutes N` (default 30) | none |
| `init` | `scripts/bootstrap_secrets.py` | `--dir DIR` `--env-file PATH` `--template PATH` `--check` | see §8.1 |
| `doctor` | `scripts/preflight.py` | `--dev` `--offline` `--dir DIR`; **new** `--server URL` for the backend check | see §8.2 |
| `completion SHELL` | (none) | `bash zsh fish powershell` | new |

Output differences, stated: rich styling (bold/colour via markup) is gone; colour is an ANSI helper active only on a tty with `NO_COLOR` unset. Untrusted text (git stderr, server bodies) is written with `fmt.Fprint` and never interpreted, so `[rejected]` and `[/x]` survive structurally. `--version` prints the §4.3 line instead of click's `lazyaf, version 0.2.0`. Dynamic completion may hit the backend on TAB (§7).

The HTTP surface called is unchanged: `GET/POST /api/repos`, `POST /api/repos/ingest`, `GET /api/repos/{id}`, `GET /api/repos/{id}/branches`, `GET /api/repos/{id}/clone-url`, `POST /api/test-refs/reconcile`, `/api/debug`, `/api/debug/{sid}`, `/{sid}/abort|extend|join-token|resume|terminal`, `POST /api/pipeline-runs/{run_id}/debug-rerun`. Typed structs in `internal/api` for `RepoIngest {id,name,internal_git_url,clone_url}`, branches `{branches[{name,commit,is_default,is_lazyaf}],default_branch,total}`, reconcile `{created,updated,orphaned}`, `DebugSessionInfo` (`backend/app/schemas/debug.py`). All unauthenticated today.

---

## 6. `install.sh`

Lives at `cli/install.sh` (committed, shellchecked) **and** is attached to every release. The README line is `curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash` — `releases/latest/download/<asset>` is GitHub's stable redirect to the newest non‑prerelease, so the script and the asset names it expects are versioned together (a raw‑main script could name assets an older release does not have). `LAZYAF_VERSION=v0.3.0 curl ... | bash` pins; `LAZYAF_INSTALL_DIR` overrides the target; `--no-completions` skips completion; `--from-dir DIR` (local mode, for tests and CI) reads a dist directory instead of the network. Until v0.3.0 is cut that URL 404s; the README edit lands in P3 with the line marked "from v0.3.0".

**Asset naming (fixed contract, asserted by the release smoke).** Bare binaries, no archive, no tar/unzip dependency: `lazyaf_<ver>_linux_amd64`, `lazyaf_<ver>_linux_arm64`, `lazyaf_<ver>_darwin_amd64`, `lazyaf_<ver>_darwin_arm64`, `lazyaf_<ver>_windows_amd64.exe`, `lazyaf_<ver>_windows_arm64.exe` (version without the `v`), plus a **version‑free** `checksums.txt` (`sha256sum` format), plus `install.sh`, `docker-compose.release.yml`, `.env.example`.

**End to end.**
1. `set -euo pipefail`; bash 3.2‑compatible (macOS); needs `curl` and `sha256sum` or `shasum`. A missing tool is a refusal naming it and the manual path; verification is never skipped and there is no `--insecure`.
2. Platform: `uname -s` → `Linux`, `Darwin`, `MINGW*|MSYS*|CYGWIN*` → `windows` (Git Bash, which is what the owner's Bash tool is); `uname -m` → `x86_64|amd64` → `amd64`, `aarch64|arm64` → `arm64`. Anything else: `no prebuilt lazyaf for <os>/<arch>; supported: <the six>; build from source: go install github.com/Brennan-VanderLaan/lazyaf/cli/cmd/lazyaf@<tag>` — exit 1. (This is why the root `go.mod` matters.)
3. Version: `$LAZYAF_VERSION`, else `latest` resolved by downloading `releases/latest/download/checksums.txt` and reading the version out of the first filename in it — no GitHub API call, no token, no 60/hour rate limit, and `latest` never resolves to a prerelease. Prints `installing lazyaf v0.3.0 (latest)`.
4. Into `mktemp -d` (trap cleanup): the one binary; verify with `sha256sum -c --ignore-missing` or `shasum -a 256 -c` against `checksums.txt`; on mismatch print expected vs actual, the URL, and stop. A 404 prints the full URL and "that version has no asset for <os>/<arch> — see the releases page".
5. `chmod +x`; `mkdir -p "${LAZYAF_INSTALL_DIR:-$HOME/.local/bin}"`; write to `$DIR/lazyaf.tmp.$$` then `mv -f` over `$DIR/lazyaf[.exe]` (readers see old or new, never half). On Windows a RUNNING `lazyaf.exe` cannot be replaced: the script moves the old one to `lazyaf.exe.old` first, moves the new one in, deletes `.old` on the next run; if even that fails: "close every running lazyaf (or a shell whose completion is calling it) and re‑run".
6. Upgrade/reinstall: if a `lazyaf` already exists in the target dir, print `lazyaf 0.2.9 -> 0.3.0`; if equal, print `0.3.0 is already installed; reinstalling` and continue (idempotent, so a broken binary is repaired by re‑running). If a `lazyaf` exists elsewhere on PATH ahead of the target dir (`command -v lazyaf` != target), warn that the shell will keep finding that one and name it.
7. Post‑install proof: run the installed path's `--version --short`; if it is not the requested version, exit 1 ("installed binary reports X, expected Y") — the checksum proved bytes, this proves the bytes run here.
8. PATH: `case ":$PATH:" in *":$DIR:"*)` ok; else print the one‑line fix for `$SHELL` (bash → `export PATH="$HOME/.local/bin:$PATH"` in `~/.bashrc`; zsh → `~/.zshrc`; fish → `fish_add_path ~/.local/bin`) and NEVER edit an rc file. On Git Bash also print the Windows note: add `%USERPROFILE%\.local\bin` to the user PATH via Settings, or `[Environment]::SetEnvironmentVariable('Path', "$env:USERPROFILE\.local\bin;" + [Environment]::GetEnvironmentVariable('Path','User'), 'User')` in PowerShell — and explicitly NOT `setx PATH` (it truncates at 1024 characters). `$HOME` on the owner's box contains a space: every path expansion stays double‑quoted, and `install_test.go` runs once under a temp `HOME` containing a space.
9. Completion (§7): bash → written; zsh/fish/powershell → one‑liners printed. On Git Bash, `bash-completion` is not shipped by Git for Windows, so the script checks for the loader (`/usr/share/bash-completion/bash_completion` or `$BASH_COMPLETION_USER_DIR`) and, when absent, prints `source <(lazyaf completion bash)` for `~/.bashrc` instead of writing a file nothing will load.
10. Every failure names what was attempted, the URL or path, and one remedy; every step prints what it did (`downloaded <url>`, `verified sha256 <first 12>`, `installed <path>`).

**Tested** (TG, `cli/internal/installsh/install_test.go`, running `bash cli/install.sh --from-dir <dist> --install-dir $(mktemp -d) --no-completions`): verify passes and the version line matches; a corrupted byte fails before anything is installed; unknown arch (via `LAZYAF_FORCE_PLATFORM=plan9/mips`) refuses naming the matrix; second run says "already installed; reinstalling"; PATH warning appears when the dir is absent from PATH and not when present; the `.tmp` file never survives. `bash -n cli/install.sh && shellcheck cli/install.sh` runs in `pr-build` (shellcheck is on ubuntu‑latest; no third‑party action). Requires `bash` on the TG host — present in the image (Debian) and on the owner's box (Git Bash).

**README/QUICKSTART text** (replaces README.md:179 and QUICKSTART.md:283-296):

> **Install the CLI** (from v0.3.0)
> ```
> curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash
> ```
> Installs a single static binary into `~/.local/bin` after verifying its sha256 against the release's `checksums.txt`, and tells you the one line to add if that directory is not on your PATH. Pin a version with `LAZYAF_VERSION=v0.3.0`. Re‑running upgrades (or repairs) in place.
> **Windows:** run the same line in Git Bash (it installs `lazyaf.exe` into `%USERPROFILE%\.local\bin`), or download `lazyaf_<ver>_windows_amd64.exe` and `checksums.txt` from the release page, verify with `Get-FileHash -Algorithm SHA256`, and put the folder on your user PATH.
> The binary needs no Python. The one exception is `lazyaf tests reconcile --from-collect`, which runs a collector *inside your pytest* and therefore needs the interpreter your suite uses.

---

## 7. Completion

`lazyaf completion bash|zsh|fish|powershell` prints cobra's generated script (`GenBashCompletionV2(w, true)`, `GenZshCompletion`, `GenFishCompletion`, `GenPowerShellCompletionWithDesc`). V2 delegates to the hidden `__complete` command, so subcommands, flags and help strings complete without a hand‑maintained script and cannot drift from the command tree. File‑path completion for `ingest REPO_PATH`, `--refs`, `--collect-path` via `MarkFlagFilename`/`MarkFlagDirname`.

Dynamic values via `ValidArgsFunction` / `RegisterFlagCompletionFunc`: repo ids for `land`, `branches`, `tests reconcile` (`GET /api/repos`, shown as `id<TAB>name`); session ids for every `debug` verb except `rerun` (`GET /api/debug`); `--remote` from `git remote`; `--branch` on `land` from `GET /api/repos/{id}/branches` when the id is already on the line. Network lookups use the resolved server with a **1 s timeout** and on any failure return `ShellCompDirectiveError` with zero candidates — a dead `LAZYAF_SERVER` costs one second per TAB and never hangs the shell. This is the one deliberately quiet path in the binary, for a structural reason (a completion callback cannot print without corrupting the command line); `internal/cmd/completion.go` says so in a comment, and `LAZYAF_COMPLETE_NO_REMOTE=1` disables the remote lookups. Not completed, stated: `debug rerun RUN_ID` (no listing endpoint) and `--break` step keys (needs run → pipeline → graph lookups).

Where `install.sh` puts it: bash → `${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/lazyaf` (bash‑completion 2.x autoloads it; no rc edit), unless the loader is absent (Git Bash), in which case the `source <(lazyaf completion bash)` line is printed. zsh → printed `fpath` instruction (the script cannot know the user's fpath). fish → printed `lazyaf completion fish > ~/.config/fish/completions/lazyaf.fish`. powershell → printed `lazyaf completion powershell | Out-String | Invoke-Expression` for `$PROFILE`. A user without `install.sh` runs `lazyaf completion --help`, which documents all four one‑liners.

Tests (TG): `cmd.TestCompletionScriptsParse` generates all four; runs `bash -n` on the bash one and asserts the zsh one starts with `#compdef lazyaf`; `cmd.TestEveryLeafCommandIsCompletable` asserts every leaf appears in the bash and zsh output, and `lazyaf __complete debug ""` lists the seven debug verbs; `cmd.TestDynamicCompletion` drives `__complete land ""` against an `httptest.Server` speaking the real `/api/repos` shape and asserts the ids and directive; `cmd.TestDynamicCompletionServerDown` asserts the error directive within the timeout against a closed port. The macOS release smoke sources the bash script under `/bin/bash` (3.2) with `complete -p lazyaf` — cobra's V2 script under Apple's bash 3.2 is **UNVERIFIED**; a red macOS smoke there means documenting `bash-completion@2`/zsh for macOS, not weakening the check.

---

## 8. `init` / `doctor`

### 8.1 `lazyaf init [--dir DIR] [--env-file PATH] [--template PATH] [--check]` — every property `test_bootstrap_secrets.py` pins

| Property (Python test) | Go carrier |
|---|---|
| creates `.env` from the template when missing (`:70`) | `initcmd.TestCreatesEnvFromTemplate` |
| generates both secrets with real entropy, ≥ 43 chars, URL‑safe (`:85`) | `envfile.TestGenerateSecretShape` — 48 bytes `crypto/rand`, `base64.RawURLEncoding` = 64 chars, the alphabet of `token_urlsafe(48)` (`bootstrap_secrets.py:72-73, :151-153`), so a `.env` written by either tool is indistinguishable |
| two installations do not share a secret (`:95`) | `envfile.TestTwoRunsDiffer` |
| rerunning changes nothing (`:107`) | `initcmd.TestIdempotent` |
| an existing value is never overwritten (`:118`) | `envfile.TestKeptIsKept` |
| unrelated keys and comments survive (`:129`) | `envfile.TestUnrelatedLinesSurvive` |
| an empty assignment is filled in place (`:150`) | `envfile.TestEmptyAssignmentFilledInPlace` (quotes and `export ` handled by `SplitAssignment` = `:164-180`; the LAST active assignment is replaced, `:241-246`) |
| a retired default or placeholder is replaced (`:166`) | `envfile.TestPlaceholderReplaced` over `RETIRED_PUBLIC_SECRETS` (`:91-96`), `PLACEHOLDER_SECRETS` (`:101-121`, generated — §8.3), all‑`x`, blank |
| `<KEY>_FILE` in the file or ambient env → delegated, nothing written (`:176`) | `envfile.TestFilePointerDelegates` |
| no generated secret is ever printed; existing never printed back; `--check` neither (`:191, :198, :205`) | `initcmd.TestNeverPrintsASecret` — captures stdout+stderr in normal and `--check` modes; asserts no value substring and no 64‑char `[A-Za-z0-9_-]` run. **Structural**: `envfile.Secret` is `struct{ v string }` whose `String`/`GoString`/`Format`/`MarshalText` all return `<redacted>`; only `WriteTo(w)` yields bytes, used by the writer alone. `envfile.TestSecretRedactsUnderEveryVerb` checks `%v %s %+v %#v` |
| `--check` reports MISSING / ok / ok (via `_FILE`), changes nothing, exits 1 on missing (`:221, :234, :239`) | `initcmd.TestCheckMode` (three subtests) |
| six concurrent runs leave one consistent file (`:249`) | `initcmd.TestConcurrentRuns` — **six real processes** (`exec.Command(os.Args[0], "-test.run=TestHelperProcess")` with `LAZYAF_TEST_HELPER=init`, the standard Go re‑exec idiom; goroutines would not exercise `O_EXCL` across processes, which is the point at `:249-277`); asserts both keys present, ≥ 43 chars, no `.tmp-*`/`.lock` leftovers |
| a value written between read and write is not clobbered (`:280`) | `initcmd.TestReReadInsideTheLock` |
| a stale lock does not wedge the next run (`:299`) | `envfile.TestStaleLockIsBroken` — lock `<env>.lazyaf-bootstrap.lock` via `os.OpenFile(O_CREATE|O_EXCL|O_WRONLY, 0600)`, 20 s timeout, 50 ms poll, older than 120 s → unlinked and retried (`:123-125, :277-335`) |
| runs on a bare interpreter (`:330`) | **retired** `python-interpreter:` a static binary has no interpreter; the release smoke on a bare runner is the equivalent |
| default env file is the repo root in a checkout (`:345`) | `initcmd.TestRootMarkers` — `--dir` (default CWD) must contain one of `_ROOT_MARKERS` (`.env.example`, `docker-compose.release.yml`, `docker-compose.yml`, `:46`) |
| standalone download writes beside itself (`:354`) | **retired** `semantics-changed:` the binary lives in `~/.local/bin`, so "beside itself" is meaningless; replaced by `initcmd.TestRefusesUnmarkedDirectory`: without a marker init REFUSES — "this does not look like a LazyAF directory (no .env.example / docker-compose*.yml); run it in your checkout or the folder holding docker-compose.release.yml, or pass --env-file" |

Atomic write: sibling `.tmp-<pid>` created `O_EXCL` 0600, written, `Sync()`, `os.Rename` (on Windows `MoveFileEx(MOVEFILE_REPLACE_EXISTING)`, the same replace semantics as `os.replace`, `:338-360`); chmod 0600 best‑effort as `_restrict_permissions` (`:363-368`). CRLF detected and preserved (`:193-199`). Appended block header reads `# --- generated by lazyaf init ---`. Exit 0/1/130.

### 8.2 `lazyaf doctor [--dev] [--offline] [--dir DIR] [--server URL]` — every property `test_preflight.py` pins, plus the rest of `preflight.py`

Same checks, same **order** (`preflight.py:672-735`: compose file, docker CLI/daemon/compose v2, disk, env keys + shared secrets, ports, service images, step images), same `[ OK ]/[WARN]/[FAIL]` vocabulary (`:62-73`), same "docker down → stop and only check env" short‑circuit (`:693-704`), same exit rule (fails → 1; WARN never fails, `:713-728`). Plus **one** new check, `check_backend`: `GET <server>/health` with the `describe_server` provenance line, because "which URL" is the most common support question.

| Property | Go carrier |
|---|---|
| every valid `[HOST_IP:]PORT` form resolves (`TestTheFormsAValidValueTakes::test_it_resolves`, 20 cases) | `doctor.TestPortForms` (same table, `resolve_port` grammar `:437-476`) |
| the shipped `.env.example` values parse (`:78`) | `doctor.TestEnvExampleValuesParse` — walks up to the repo root for `.env.example`, `t.Fatal` if absent |
| genuinely wrong forms refused, out of range refused, the refusal never advises removing the host IP (`:107-116`) | `doctor.TestPortRefusals` |
| `port_in_use` = connect to 127.0.0.1 then bind, **no `SO_REUSEADDR`** (`:406-434`) | `doctor.TestBindProbeSeesARealListener` (binds a real listener, expects "in use" on Linux and Windows) |
| docker via subprocess only (`:98-136`), `docker image inspect` / `manifest inspect` (`:526-539`), pull+retag remedies (`:632-643`) | `doctor.TestDockerChecksAreSubprocessOnly` (exec recorder) |
| disk WARN 15 GB / FAIL 5 GB (`:58-59`) | `doctor.TestDiskThresholds` |
| env: `.env` missing → FAIL naming `lazyaf init` (`:234-243`); `git check-ignore` (`:251-262`); shape‑only descriptions, values never printed (`:213-226`); shared secrets: `_FILE` satisfies, retired default → FAIL, placeholder → FAIL, < 32 chars → WARN (`:327-391`) | `doctor.TestEnvVerdicts`, `doctor.TestNoValueIsEverPrinted` |
| ONE `.env` parser shared with init | `envfile.ReadValues` used by both (the script had two: `bootstrap_secrets.py:164-190` and `preflight.py:173-194`) |
| no side effects: pulls nothing, writes nothing, starts nothing (`:8-11`) | `doctor.TestNoSideEffects` (exec recorder + temp‑dir watch: no `pull`/`run`/`up`, no writes) |
| never a traceback (`:731-735`) | top‑level `recover()` → "doctor hit an unexpected problem and stopped: <err>. This is a bug in lazyaf doctor, not a problem with your setup. Please report it: <issues url>", exit 1; `--debug-trace` prints the stack |

### 8.3 The two lists that must not become third copies — generated, currency‑checked in T1

`scripts/gen_cli_contracts.py` (stdlib; imports `scripts/build_images.py`'s `IMAGES` and `backend/app/config.py`'s `_PLACEHOLDER_SECRETS` the way `.github/scripts/step_images.py` already imports the table) renders two files with a DO‑NOT‑EDIT header naming the generator:
- `cli/internal/doctor/step_images_gen.go` — `var StepImages = []string{"lazyaf-base:dev", ...}` from `IMAGES` (`build_images.py:74-84`). `preflight.py`'s `FALLBACK_STEP_IMAGES` (`:42-47`) — already a second copy — dies.
- `cli/internal/envfile/placeholders_gen.go` — `PlaceholderSecrets` and `RetiredPublicSecrets` from `backend/app/config.py` and `bootstrap_secrets.py:91-121`, which today are "kept in sync by eye" (`:98-100`).

`tdd/unit/scripts/test_cli_contracts_current.py` (T1) renders both in‑process and asserts byte equality with the committed files; a table change without `python scripts/gen_cli_contracts.py` fails T1 naming the command. Same idiom as the codec corpus, one generator, two outputs.

Consequences elsewhere: `scripts/preflight.py`/`bootstrap_secrets.py` leave `release.yml`'s asset list; the release notes and `docker-compose.release.yml`'s header (`:7, :27-29, :97`) say `lazyaf init` / `lazyaf doctor`; the compose fail‑fast messages `:?not set. Run: python scripts/bootstrap_secrets.py` (`docker-compose.yml:40-41, :107, :200-201, :241`; `docker-compose.dev.yml:17-18`; `docker-compose.qa.yml:45-46`; `docker-compose.release.yml:102-103, :164`) and `backend/app/config.py:133, :205` become `Run: lazyaf init`. Adding `bootstrap_secrets.py` and `preflight.py` to `FORBIDDEN` forces every one of those (all are `SEARCH_ROOTS`).

---

## 9. Release + PR CI

### 9.1 `.github/workflows/release.yml` (rewritten; the header contract "packages, never gates" — `:5-17` — keeps its wording minus the wheel paragraphs)

`on: push: tags: ['v*']` + `workflow_dispatch` (the `publish_to_pypi` input is deleted). Top‑level `permissions: contents: read`.

- **`secret-scan`** — `uses: ./.github/workflows/secret-scan.yml`, unchanged.
- **`build`** (needs secret‑scan; ubuntu‑latest; `contents: read`): `actions/checkout` (existing SHA pin, `fetch-depth: 0`, `persist-credentials: false`) → `actions/setup-go` (**new action**, pinned by full SHA with a `# vN.N.N` comment; added to `WORKFLOWS.md`'s pinned list `:92-99`; `go-version-file: go.mod`, `cache: true`) → `go mod verify` → `go mod tidy && git diff --exit-code go.mod go.sum` → `go vet ./...` → `bash scripts/build_cli.sh` (all six + `checksums.txt`; `Version=dev` on a dispatch without a tag) → `python3 .github/scripts/check_binary_version.py --tag "$GITHUB_REF_NAME" --dist cli/dist` (tag refs only, as today `:120-123`) → `actions/upload-artifact` `lazyaf-release-assets` (`cli/dist/*` + `cli/install.sh`). One job builds all six so ONE `checksums.txt` is written by the shell that built the files — no artefact stitching.
- **`smoke`** (needs build; `fail-fast: false`; matrix `ubuntu-latest`, `macos-latest`, `windows-latest`; `contents: read`): download the artefact; `bash cli/install.sh --from-dir assets --install-dir "$RUNNER_TEMP/bin" --no-completions` (on windows‑latest `bash` is Git Bash — the same shell the owner uses); then on the installed binary in a directory with NO checkout context: `--version --short` == `${TAG#v}` (tag runs), `--help` exit 0, `completion bash | bash -n` (macOS: sourced under `/bin/bash` + `complete -p lazyaf`), `init --check --env-file "$RUNNER_TEMP/nope"` exits 1 printing `MISSING`, `doctor --help`. This is today's fresh‑venv smoke (`:125-133`) made stronger: it proves installer + asset names + checksums + the real OS binary before anything is published. **No `go test` here** — `pr-build.yml:5-11` and `WORKFLOWS.md` rule 1: GitHub packages, the dogfood pipeline gates.
- **`publish`** (needs smoke; `permissions: contents: write` — still the single elevated job): download; collect assets = six binaries + `checksums.txt` + `cli/install.sh` + `docker-compose.release.yml` + `.env.example` (warn‑not‑fail on missing, as `:135-153` does; `preflight.py`/`bootstrap_secrets.py` removed from the loop at `:146`); release notes from a file, onboarding first: "## Install the CLI" = the curl|bash line pinned with `LAZYAF_VERSION=${TAG}` + the Windows manual line; "## Run the stack" = download the two compose assets, `lazyaf init`, `echo LAZYAF_VERSION=<tag> >> .env`, edit API keys, `lazyaf doctor`, `docker compose -f docker-compose.release.yml pull && up -d`; GHCR paragraph unchanged; "## Provenance" = version, commit, "LazyAF's own dogfood pipeline tested this revision; GitHub Actions only packaged it"; `gh release view || create --verify-tag` / `upload --clobber` exactly as `:204-229`.
- **DELETED**: `wheel` (`:81-237`), `pypi` (`:267-308`) and its `environment: pypi` / `id-token: write`, the `lazyaf-cli-dist` artefact.

### 9.2 `.github/workflows/pr-build.yml`

`cli-wheel` (`:80-120`) → `cli-binary`: setup‑go (same SHA pin), `go mod verify`, `go mod tidy && git diff --exit-code go.mod go.sum`, `go vet ./...` (compile‑class, not a test — stated), `bash scripts/build_cli.sh` (all six, proves cross‑compilation with no CGO), smoke the host binary (`--version --short` matches `^dev\+[0-9a-f]{7}` — proves dev builds say dev; `--help`), `bash -n cli/install.sh && shellcheck cli/install.sh`, upload `lazyaf-cli-binaries` (14‑day retention as today). `step-images` (`:135-180`) and `service-images` (`:182-`) untouched in text; the test‑runner Dockerfile change flows through `build_images.py`'s table automatically (`:123-134`); budget +2–3 min for the Go tarball under the existing 45‑minute timeout.

### 9.3 Everything else

- `release-please.yml`: unchanged; its `package` job (`:151-200`) dispatches `release.yml` and `images.yml` at the tag as today.
- `images.yml`: untouched; the test‑runner content hash moves once, so one rebuild/publish happens on the merge. The Go layer ships in `ghcr.io/.../test-runner` — correct, a user repo's pipeline may run Go tests on it.
- `.github/dependabot.yml`: add
  ```yaml
    - package-ecosystem: "gomod"
      directory: "/"
      schedule: { interval: "monthly" }
      groups: { gomod: { patterns: ["*"] } }
      labels: ["dependencies", "cli"]
      commit-message: { prefix: "build(cli)" }
      open-pull-requests-limit: 3
  ```
  with a comment acknowledging the file's own argument (`:9-20`): Go deps are the product's supply chain like pip's; the difference is they are hash‑pinned in `go.sum`, `go mod verify` runs before merge, and a merged bump is tested after merge by TG on the push to main — exactly like a merged actions bump today. `build` is a patch bump under CONTRIBUTING's table; the `tool` directive means gotestsum rides the same PR.
- `.gitignore`: `cli/bin/`, `cli/dist/`. `scan_repo_secrets.py:61-63` scans tracked files under 4 MiB; a gitignored binary is never tracked. `go.sum` is tracked (hashes only) — no vendoring. P0 runs the scanner over the corpus (base64 of `bytes(range(256))` must not trip a key pattern).
- `.github/WORKFLOWS.md`: `:12, :28, :30, :47, :56, :73-74, :98, :252-260, :267-291, :346, :438, :456-457, :500, :528, :587-598, :616-618` all reference the wheel/PyPI/`__init__.py` and are rewritten; the security‑posture table gains `release.yml → publish : contents: write`.

---

## 10. The dogfood Go tier (TG)

### 10.1 Image — `images/test-runner/Dockerfile`, after the uv install

```dockerfile
ARG GO_VERSION=1.26.8
ARG GO_SHA256_AMD64=d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b
ARG GO_SHA256_ARM64=211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) sum="$GO_SHA256_AMD64";; arm64) sum="$GO_SHA256_ARM64";; \
      *) echo "no Go build for $arch" >&2; exit 1;; esac; \
    curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz"; \
    echo "$sum  /tmp/go.tgz" | sha256sum -c -; \
    tar -C /usr/local -xzf /tmp/go.tgz; rm /tmp/go.tgz
RUN apt-get update && apt-get install -y --no-install-recommends shellcheck && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/local/go/bin:$PATH" GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0
```

and `go version && shellcheck --version` added to the existing verify `RUN`. `HOME=/workspace/home` (base Dockerfile) means `GOMODCACHE`/`GOCACHE` land on the worker's persistent workspace volume, so the ~5 modules download once per worker (the runner already needs egress for `uv sync`); no pre‑warm and no staged `go.mod` in the image context (that would restamp the image on every dependency bump — DISTRIBUTION‑FIRST's approach, rejected). No `IMAGES` row change (`build_images.py:83`); the content hash restamps the image and `--check` reports stale. `tdd/unit/control_runtime/test_image_contract.py` gains `test_test_runner_go_matches_go_mod_toolchain`: parse `ARG GO_VERSION=` from the Dockerfile and `toolchain go` from `go.mod`, refuse divergence.

### 10.2 Tier — `scripts/run_tier.py`

`TIERS["TG"] = {"name": "Go CLI: contract, unit, install.sh (no Docker)", "kind": "go", "cwd": REPO_ROOT, "preflight": {"argv": ["bash", "scripts/build_cli.sh", "--host-only"], "cwd": REPO_ROOT, "fix": "bash scripts/build_cli.sh --host-only"}, "argv": ["go", "tool", "gotestsum", "--junitfile", "<junit>", "--format", "testname", "--", "./...", "-count=1", "-shuffle=on"], "junitxml": "junit-tg.xml"}`. `run_tier()` (`:149-200`) gains a `kind` switch (the three pytest tiers get `kind: "pytest"`) and a per‑spec preflight `cwd` (today hard‑coded to `BACKEND_DIR`); the stale‑junit unlink (`:167-175`), the red‑stays‑red return and the `ci_gate.py` call are shared unchanged. `-count=1` defeats Go's test cache (a cached PASS did not execute — the fake green the gate's max‑age rule exists for); `-shuffle=on` because order dependence in a fresh suite is cheapest to find now; `-mod=readonly` so a test can never rewrite `go.sum`. No `-race` (CGO). `tdd/unit/scripts/test_run_tier.py` (new, T1) pins `kind: go`, `-count=1`, `--junitfile`, and that every pytest tier is untouched. `scripts/test.sh`/`test.ps1 tier TG` pass through unchanged; `all` gains TG (a host without `go` gets run_tier's loud failure naming the install).

### 10.3 junit and floor

gotestsum's junit has one `<testcase classname="<import path>" name="TestX/subtest">` per test AND subtest, `<failure>` with output, `<skipped message="…">` — which `tally()` (`ci_gate.py:50-66`) and `extract_skip_reason` (`:37-47`) already read. Two properties are **verified on the first real run in P1, not asserted from memory**: (a) a `t.Skip` reason lands in the `message` attribute; (b) gotestsum still writes the file when a package fails to compile (if not, the unlink + ci_gate's "cannot read results" make it a loud failure anyway). House rule: **zero `t.Skip` in the Go suite**; a test that cannot run here `t.Fatal`s with the reason (a skip is the side door the skip baseline exists to close for pytest). `TestFromCollect` runs REAL `python -m pytest` and is red on a dev box without pytest — by design; the tier image has it.

Floor: `"TG": {"floor", "measured", "measured_on", "note"}` in `tdd/tier_floors.json`, written by the **INTEGRATOR only**, from the first GREEN container run, measured minus ~2%, with the note stating the absolute margin (the tier starts small — expected 300–450 testcases counting subtests — so a whole‑package compile failure is caught but a single deleted table test may not be; same property T3 has). Until that run exists `ci_gate.py:129-132` refuses the tier ("has no floor"), which is correct for a tier never measured.

**T1 goes DOWN in the deletion commit and that is written, not hidden.** Deleted from T1: `test_cli_debug.py` 58 and `test_terminal_protocol_contract.py` 74 (both per the T1 floor note), `test_cli_errors.py` ~77 (PLAN.md:929), `test_cli_tests_reconcile.py` ~20, `test_bootstrap_secrets.py` 21, `test_preflight.py` 20, `tdd/unit/packaging` ~19 non‑slow (`test_wheel_build.py` is `@slow` and already outside T1). Added back: the rewritten contract test (~20), `test_cli_contracts_current.py`, `test_check_binary_version.py`, `test_run_tier.py`, the toolchain pin, the tombstone. Net ≈ −250 against measured 5710 / floor 5595. The deletion commit lowers T1 to (new measured − 2%) with a `tier_floors.json` note that names the per‑file counts **taken from the junit of the last green run before deletion**, and a PLAN.md decision‑log line reconciling T1 −N per file against TG +M per Go package via the ledger. A reviewer must reject a cutover PR whose T1 floor is untouched: it means the Python tests were not deleted.

### 10.4 Pipeline step — `.lazyaf/pipelines/test-suite.yaml`, after `tier1` (`:101-111`) and before `tier2` (`:128`)

```yaml
  - id: "tier-go"
    name: "TG: Go CLI unit + contract conformance (no Docker)"
    type: script
    config:
      image: "lazyaf-test-runner:dev"
      command: |
        python3 scripts/run_tier.py TG
    continue_in_context: true
    on_success: next
    on_failure: stop
    timeout: 900
```

After T1 because T1 holds the corpus‑freshness test (a stale corpus fails there with "re‑run the generator", a clearer first failure than TG's byte diff). Before T2 because it needs no Docker socket and must not sit in the DooD‑allowlisted block (`:112-127`). Before T3 because T3's `test_debug_rerun.py` reads `cli/bin/lazyaf`, which the TG preflight leaves on the run's workspace volume. `tdd/unit/scripts/test_verify_executor.py` pins step ids by name in fixtures, not the dogfood step count (verified by grep; `verify_executor` asserts every script step ran local + control, which a new script step on a control‑layer image satisfies). Acceptance for the gate work (R7): one green dogfood run with `tier-go` in the graph, `CI GATE [TG]: OK` in its log, and the T3 debug loop green against the Go binary — before any Python is deleted.

---

## 11. THE PARITY GATE (R2)

A ledger, machine‑checked from both sides while both sides exist, frozen and tombstoned after the cut. Nothing may be "TBD"; the table below IS the ledger's content.

`tdd/contracts/cli_parity.json`: one entry per Python test CLASS (per module‑level FUNCTION for `test_bootstrap_secrets.py`, which has no classes), keyed `file::Name`. Each value is `{"go": ["<package>.<TestFunc>", "<package>.<TestFunc>/<subtest>", ...]}` or `{"retired": "<kind>: <reason of at least 40 characters naming what replaces it>"}` with kind from the closed set `rich-specific`, `click-specific`, `python-websockets-specific`, `packaging-wheel`, `python-interpreter`, `semantics-changed`, `superseded-by-contract`, `moved-to-e2e`.

**Enforcement A** (Python, T1, lives P2 → deletion commit): `tdd/unit/scripts/test_cli_parity_ledger.py` `ast.parse`s each Python test file (no import, so it runs in the backend env without click/rich), collects `class Test*` and module‑level `def test_*`, asserts `set(ledger keys) == set(collected)` (a class added without an entry fails; a stale key fails); for every `go` reference, regex `^func (TestName)\(` over `cli/**/*_test.go` and `t.Run("subtest"` for subtests — a dangling reference fails naming it; retirement kinds validated.
**Enforcement B** (Go, TG, permanent): `cli/internal/parity/ledger_test.go` parses every `*_test.go` under `cli/` with `go/parser` (real AST) and asserts every `go` reference resolves to an existing `func TestX(t *testing.T)` (and `t.Run` literal), so a Go test that is the parity carrier for a Python class cannot be deleted later without editing the ledger: `cmd.TestLand is the parity carrier for test_cli_errors.py::TestLand; retire it in tdd/contracts/cli_parity.json with a reason or restore it`. Prints the retired share into the test log.
**The tombstone** (Python, T1, written IN the deletion commit per `test_no_legacy_code.py`'s own rule): `tdd/unit/services/test_python_cli_retired.py` hard‑codes the frozen enumeration below (so nobody can shrink the ledger after the Python files are gone), asserts ledger keys == that set, asserts the deleted paths do not exist, and greps README/QUICKSTART/WORKFLOWS/SECURITY once for the retired tokens (docs are not `SEARCH_ROOTS`, and a stale `pip install ./cli` there is the widest‑audience R1 failure).

### The ledger

**`tdd/unit/scripts/test_cli_debug.py`** (7 classes, 58 tests)

| Python class | Disposition |
|---|---|
| `TestTerminalUrl` | `terminal.TestTerminalURL` — http→ws, https→wss, trailing slash, explicit ws accepted, schemeless refused, token never in the URL |
| `TestEscapeDecoder` | `debugproto.TestEscapeDecoder` — all 8 behaviours incl. escape split across reads, doubled escape, detach keys, unknown key reported |
| `TestRunTerminal` | `terminal.TestRunTerminal` — `ScriptedSocket`/`ScriptedIO` ported as interface fakes; the Python `ScriptedSocket` validated outbound frames with the SERVER codec (`:123-135`); the Go one validates with `debugproto.Decode`, which the corpus pins — the chain holds. All 10 subtests by the Python names (full round trip, initial size announced, no resize when unknown, arbitrary bytes survive, closed frame ends with the server's reason, local EOF, detach says paused, undecodable frame reported, unknown escape key sends nothing, dropped connection reports its close reason) |
| `TestCloseDetails` | `terminal.TestCloseDetails` — handshake status reported, close code not mistaken for status, every close code has a sentence, server's reason wins, UPGRADE_REFUSED names `lazyaf debug status`, every server close code has a meaning. The two websockets‑shape tests (`:433-441`, `:457`) → **retired** `python-websockets-specific: coder/websocket has one CloseError shape; there is no rcvd-vs-flat or legacy handshake shape to read` |
| `TestConsoleIO` | `terminal.TestConsoleIO` — byte‑exact output to a binary stream; size is a positive pair or nil |
| `TestAttachCommand` | `cmd.TestDebugAttach` — `--shell` refused exit 2 with the server's sentence **read from the corpus**; unattachable refused with `attach_unavailable_reason`; `--print-credential` opens no socket; `--token` skips the mint; interactive path end‑to‑end with a fake dialer and piped stdin asserting mode `line-buffered` and the stated warning; every debug verb present. The two click‑wiring tests (`:648, :667`) → **retired** `click-specific: there is no placeholder attach to replace on the group; cobra registers the real command directly` |
| `TestWebsocketsExtra` | **retired** `python-websockets-specific: the dialer is compiled in; no optional extra exists to be absent or bounded` |

**`tdd/unit/scripts/test_cli_errors.py`** (11 classes)

| Python class | Disposition |
|---|---|
| `TestUntrustedTextIsNeverParsed` | `ui.TestVerbatimQuoting` — git `[rejected]` survives, `[/x]` survives, CRLF not doubled, empty detail not printed, summary and remedy not interpreted. The parser is gone structurally; the properties stay as tests |
| `TestNothingExitsZeroOnFailure` | `ui.TestFailNeverExitsZero` + `cmd.TestIngestRefusesNoCommits` (real `git init` in `t.TempDir()`, names the two commands that fix it) |
| `TestUsageErrorsCarryAnExample` | `cmd.TestUsageErrorsCarryAnExample` — walks every cobra command asserting non‑empty `Example`; missing required flag reprints it; missing positional too; `lazyaf lst` suggests `list`; `lazyaf zzz` suggests nothing |
| `TestBackendUrl` | `api.TestResolveServerURL` — schemeless refused with the reason, empty refused, trailing slash, message names the source, describe names both ways to change it, same rule as the terminal client |
| `TestTheServerIsQuoted` | `api.TestServerWordsQuoted` (httptest) — string detail, pydantic 422 envelope rendered, non‑JSON body shown, empty body says so, 404 with the command's words, 200 non‑JSON blames the right component |
| `TestEveryTransportFailureIsHandled` | `api.TestTransportFailures` — refused port, timeout via a hanging handler with an injectable client timeout (distinguished from refusal), DNS failure, schemeless refused before dial; never a Go panic |
| `TestOneErrorIdiom` | `api.TestOnlyAPIImportsNetHTTP` — `go/parser` over `cli/internal/**/*.go`: `net/http` imported only by `internal/api`, `coder/websocket` only by `internal/terminal` (the same "one idiom" property enforced by AST instead of by grepping for `httpx`) |
| `TestStreams` | `ui.TestStreams` — refusals and warnings on stderr, results on stdout |
| `TestIngestRefusesEarly` | `cmd.TestIngestRefusesEarly` — blank name refused locally, nonexistent branch refused before the repo is created and lists the real ones, not‑a‑repo names the remedy |
| `TestLand` | `cmd.TestLand` — refspec `refs/remotes/lazyaf/<b>:refs/heads/<b>` asserted on the recorded `gitx.Runner` args; failed `gh pr create` exits 1 after the push and does not report success; successful PR reports success; outside a clone names the remedy |
| `TestAgainstRealRich` | **retired** `rich-specific: no markup language exists in the Go binary; the property it guarded is TestVerbatimQuoting` |

**`tdd/unit/scripts/test_cli_tests_reconcile.py`** (5 classes) → `reconcile.TestRefusesAmbiguousInput` (no source, names the orphaning hazard, no env‑var or cwd fallback, both sources), `reconcile.TestRefusesResultsManifest` (refused by default, explains the partial‑run problem, `--manifest` alias, explicit opt‑in), `reconcile.TestRefusesEmptyDeclaredSet` (empty, missing, malformed), `reconcile.TestManifestHelpers` (classify, normalize dedupes keeping first path, bare list accepted), `reconcile.TestFromCollect` (REAL `python -m pytest --collect-only` over a fixture suite in `testdata/`: both marked tests found, unmarked excluded, a collection error refuses). `no_http` → an `api.Client` whose transport panics if reached.

**`tdd/unit/scripts/test_bootstrap_secrets.py`** (21 functions) → the §8.1 table: 19 ported one‑for‑one under the Python names as subtests; `test_runs_on_a_bare_interpreter_with_no_dependencies` → **retired** `python-interpreter:`; `test_standalone_download_writes_beside_itself_not_a_level_up` → **retired** `semantics-changed:` with `initcmd.TestRefusesUnmarkedDirectory` named as the replacement.

**`tdd/unit/scripts/test_preflight.py`** (2 classes) → `doctor.TestPortForms` (incl. `TestEnvExampleValuesParse`) and `doctor.TestPortRefusals` (incl. the host‑IP advice negative).

**`tdd/unit/debug/test_terminal_protocol_contract.py`** (6 classes) → `TestConstantParity` → `debugproto.TestContractConstants` (+ the Python currency test); `TestServerToClient` + `TestClientToServer` → `debugproto.TestContractFrames`; `TestRejectionParity` → `debugproto.TestContractMalformed`; `TestBase64Parity` → `debugproto.TestContractBase64`; `TestClientOnlySurface` → `debugproto.TestEscapeKeysMapToWireCommands` + the Python `TestEscapeSurfaceIsClientOnly`. Kind `superseded-by-contract` in each reason, so the ledger says the replacement is a mechanism.

**`tdd/unit/packaging/`** (9 classes: `TestWheelContents`, `TestSdistContents`, `TestFreshVenvInstall`, `TestVersionSingleSource`, `TestPublishableMetadata`, `TestEntryPoint`, `TestDependencies`, `TestPackagesAreExplicit`, `TestPackagedTreeIsClean`) → all **retired** `packaging-wheel:` with the surviving property named in each reason: version single source → `check_binary_version.py` test + `version.TestDevBuildSaysDev`; artefact carries only the product → `-trimpath`/`CGO_ENABLED=0` + the release smoke in a directory with no checkout; fresh machine can run it → the three‑OS `smoke` job; no `.env`/credential files ship → `secret-scan.yml` over every tracked file; console script exists → the smoke's `--help`.

**Not in the ledger, stated so nobody deletes it by association:** `runner-agent/tests/test_cli.py` is the runner AGENT's CLI (T1, +189 note), untouched.

The gate closes when: Enforcement A is green on the commit immediately before the deletion; Enforcement B and the whole TG tier are green after; the tombstone is green; a dogfood run shows `CI GATE [TG]: OK` and the T3 loop green against the binary; the owner's manual acceptance (§13.4) is recorded in PLAN.md.

---

## 12. Windows

Windows is a first‑class target (the owner runs Windows + Docker Desktop; the dev box is go1.21.5 windows/amd64).

**Works.** `x/term.MakeRaw` on a console handle clears line/echo/processed input and sets `ENABLE_VIRTUAL_TERMINAL_INPUT`, so arrow, Home/End, Delete and F‑keys arrive as ESC sequences straight from conhost (Windows 10 1809+) / Windows Terminal; the msvcrt table and its `\r`→`\n` rewrite (`debug_cmd.py:313-360`) are deleted. Stdout gets `ENABLE_VIRTUAL_TERMINAL_PROCESSING`; if that call fails the banner says `raw-windows (no ANSI output)` exactly as `debug_cmd.py:297` does. Both modes restored on exit via `defer`, including on Ctrl‑C and panic. Ctrl‑] is `0x1d`; Ctrl‑C in raw mode is `0x03` forwarded to the sidecar shell; escape‑then‑`d` is the local exit. Resize: no SIGWINCH, so a 500 ms poll of `term.GetSize` sends a resize frame on change (POSIX uses SIGWINCH) — one `sizeChanges(ctx) <-chan [2]int` seam, two implementations; an improvement over today's initial‑size‑only, stated. Paths: `~/.local/bin` is `%USERPROFILE%\.local\bin`; `install.sh` under Git Bash (`uname` → `MINGW64`) writes `lazyaf.exe` there. `init`/`doctor`: `O_EXCL` and `os.Rename` replace are atomic on NTFS; chmod is a no‑op as today; port probe via exclusive `net.Listen` (Windows is exclusive by default); `docker.exe`/`git.exe`/`gh.exe`/`python.exe` via `exec.LookPath` (honours `PATHEXT`). Completion: `lazyaf completion powershell` in `$PROFILE`; bash completion in Git Bash via the printed `source` line. Stdin reads: a goroutine blocks on `os.Stdin.Read`; on a console handle Go uses `ReadConsole` with UTF‑16→UTF‑8, so non‑ASCII keystrokes survive. The Go suite passes on the Windows dev box (`t.TempDir()`, `filepath.Join`, subprocess concurrency tests, build‑tagged console files driven through the `ConsoleIO` interface) — part of P1–P3's definition of done.

**UNVERIFIED and the first P1 task on the owner's box:** whether `ReadConsole` returns per keystroke with line input disabled under both Windows Terminal and legacy conhost. Fallback: `ReadConsoleInput` via `x/sys/windows` (~80 lines, fully under our control). If neither delivers arrow keys, attach on Windows is line mode with a stated warning — what the Python CLI does under `CliRunner` today, not a regression.

**Does not work, and says so.** (1) A console where VT input cannot be enabled (pre‑1809 conhost, some third‑party consoles): attach degrades to LINE mode with a printed notice — "this console cannot enter raw mode; running line‑buffered — use Windows Terminal for arrow keys" — never a silent degrade to garbage keystrokes (R1); every other verb works. (2) `install.sh` from PowerShell/cmd (no bash): README gives the manual download + `Get-FileHash` line; no `install.ps1` this wave. (3) 32‑bit and arm32: no asset; `install.sh` says so and points at `go install`. (4) windows/arm64 is built and version‑checked via `go version -m` but never executed in CI (no runner); the release notes label it "built, untested". (5) `-race` (CGO). (6) Long paths (> 260 chars) not claimed.

---

## 13. Phases, lanes, file ownership, acceptance gate, deletion list

### 13.1 Lanes — every file owned by exactly one lane

| Lane | Phases | Exclusive files |
|---|---|---|
| **L1 · CONTRACT‑PY** | P0 | `backend/app/services/execution/debug_terminal.py` (adds `export_contract`; codec untouched) · `scripts/gen_debug_terminal_corpus.py` · `tdd/contracts/debug_terminal.v1.json` · `tdd/unit/debug/test_terminal_protocol_contract.py` · `scripts/gen_cli_contracts.py` · `cli/internal/doctor/step_images_gen.go` · `cli/internal/envfile/placeholders_gen.go` · `tdd/unit/scripts/test_cli_contracts_current.py` |
| **L2 · GO‑CORE** | P1 | `go.mod` · `go.sum` · `cli/cmd/lazyaf/main.go` · `cli/internal/version/**` · `cli/internal/debugproto/**` · `cli/internal/terminal/**` · `cli/internal/ui/**` · `cli/internal/api/**` · `scripts/build_cli.sh` · `.gitignore` · `cli/README.md` |
| **L3 · GO‑COMMANDS** | P2 | `cli/internal/cmd/**` · `cli/internal/gitx/**` · `cli/internal/reconcile/**` · `tdd/e2e/test_debug_rerun.py` |
| **L4 · GO‑ONBOARD** | P3 | `cli/internal/initcmd/**` · `cli/internal/doctor/**` (except `step_images_gen.go`) · `cli/internal/envfile/**` (except `placeholders_gen.go`) |
| **L5 · GATE** | P1 | `images/test-runner/Dockerfile` · `scripts/run_tier.py` · `tdd/unit/scripts/test_run_tier.py` · `tdd/unit/control_runtime/test_image_contract.py` · `.lazyaf/pipelines/test-suite.yaml` · `scripts/test.sh` · `scripts/test.ps1` |
| **L6 · RELEASE** | P3, P4 | `.github/workflows/release.yml` · `.github/workflows/pr-build.yml` · `.github/scripts/check_binary_version.py` · `.github/scripts/check_release_version.py` (deleted P4) · `tdd/unit/scripts/test_check_binary_version.py` · `cli/install.sh` · `cli/internal/installsh/**` · `.github/dependabot.yml` · `.github/release-please-config.json` (P4 only) · `.github/WORKFLOWS.md` |
| **L7 · PARITY+DOCS** | P2, P3, P4 | `tdd/contracts/cli_parity.json` · `tdd/unit/scripts/test_cli_parity_ledger.py` · `cli/internal/parity/**` · `tdd/unit/services/test_no_legacy_code.py` · `tdd/unit/services/test_python_cli_retired.py` · `README.md` · `QUICKSTART.md` · `CONTRIBUTING.md` · `SECURITY.md` · `docker-compose.yml` · `docker-compose.dev.yml` · `docker-compose.qa.yml` · `docker-compose.release.yml` · `backend/app/config.py` (`:133`, `:205` only) · `backend/pyproject.toml` (`rich` line only) · `PLAN.md` |
| **INTEGRATOR** | gate, P4 | `tdd/tier_floors.json` · `tdd/skip_baseline.json` · the deletion commit itself (the `git rm` of everything in §13.5) — nobody else touches floors or deletes |

L4 writes against L2's `ui`/`api` contracts as written in §2.3 and §5; L3 writes against L2's `debugproto`/`terminal` interfaces (`Socket`, `ConsoleIO`, `RunTerminal(ctx, Socket, ConsoleIO, notice) (Result, error)`). Cross‑lane sequencing: L1 → L2 (contract_test needs the corpus) → L3, L4 in parallel → L5 can start with L2 (the tier needs a compiling module) → L6 needs L2's `build_cli.sh` → L7's ledger needs L3/L4's test names, its docs need L6's install line.

### 13.2 Phases

**P0 — Contract artefacts, Python only, additive. (L1)** `export_contract()`, the generator, the corpus, the rewritten contract test keeping its cross‑import classes (proves the corpus is a faithful transcription of the wire that ships today), `gen_cli_contracts.py` and its two Go outputs (which compile only once P1's packages exist — until then they are plain files T1 checks byte‑for‑byte), the T1 currency test. Run `scan_repo_secrets.py` over the corpus. Commit `test:`. Green because nothing is removed; T1 gains ~25.

**P1 — Go module skeleton + codec + terminal + TG tier. (L2, L5)** Root `go.mod`/`go.sum`; `version`, `debugproto` (contract_test passing the corpus byte‑exact), `terminal` (RunTerminal + fakes + dialer with `SetReadLimit` pin + console files), `ui`, `api`; `build_cli.sh`; `.gitignore`; Dockerfile Go layer + shellcheck; `run_tier.py` `kind: go` + TG spec + its T1 test; toolchain‑pin test; `tier-go` step; INTEGRATOR stamps the TG floor from the first green container run. **First task, on the owner's box: the Windows raw‑console spike (§12).** Verify on the first real run: gotestsum's `<skipped message>` shape and its behaviour on a compile failure; that `tdd/unit/packaging`'s `packaged_files` walk (`test_no_tests_in_the_build_context`) does not object to `_test.go` files under `cli/` (a judge verified it matches `test_*.py`/`*_test.py` only; confirm by running it). Commits `build(cli):`/`ci:`. The Python CLI is untouched and still what users install.

**P2 — Every command + the ledger + T3 on the binary. (L3, L7)** `cmd`, `gitx`, `reconcile` with the embedded plugin; completion; `--token`; persistent `--server`. `cli_parity.json` complete (every entry `go` or `retired` — a ledger with a TODO fails Enforcement A), Enforcement A (T1) and B (TG). `test_debug_rerun.py` switches to the subprocess (§3.6). Both CLIs ship in the tree; docs still say pip. Commit `build(cli):`.

**P3 — init + doctor + release CI + install.sh + docs cutover. (L4, L6, L7)** `envfile`/`initcmd`/`doctor` with every §8 property; `release.yml` rewritten with `build`/`smoke`/`publish` (the `wheel` job is left in place — a dispatch on a branch runs both so the new path can be compared to the old; **no tag is cut**); `pr-build` `cli-binary`; `check_binary_version.py` + test; `install.sh` + its TG test; dependabot; README/QUICKSTART/CONTRIBUTING/SECURITY/WORKFLOWS/compose headers and messages/`config.py`/`cli/README.md` switched to the binary, `lazyaf init`, `lazyaf doctor`. A `workflow_dispatch` of `release.yml` on the branch proves six binaries, `checksums.txt`, the three‑OS smoke, and — one deliberate negative run — that `check_binary_version.py` refuses a wrong `-X`. Commits `build(cli):`/`ci:`/`docs:`.

**⟶ ACCEPTANCE GATE (§13.3). Nothing below runs until every line passes.**

**P4 — DELETE, one commit, `feat(cli)!: replace the Python CLI with a Go binary; lazyaf init / lazyaf doctor replace the onboarding scripts`. (INTEGRATOR, L6, L7)** Everything in §13.5; the contract test drops its cross‑import classes; Enforcement A deleted with the classes it counted; the tombstone written in this commit; `test_no_legacy_code.py`: `SEARCHABLE_SUFFIXES += ".go"`, `FORBIDDEN += "bootstrap_secrets.py", "preflight.py", "lazyaf_cli", "lazyaf-cli", "pip install ./cli", "lazyaf.debug_cmd", "lazyaf.debug_protocol"`; `release-please-config.json` loses `extra-files`; `check_release_version.py` deleted; `release.yml` loses `wheel`/`pypi`; `pr-build.yml` loses `cli-wheel`; `rich` leaves the backend test extra; T1 floor lowered with the reconciliation note; TG re‑stamped; PLAN.md decision log + Numbers table. `images/debug-sidecar/motd` (`:20, :28`) is untouched — it names subcommands that still exist. Post‑merge checklist: re‑dispatch `release-please.yml`, confirm the refreshed release PR reads 0.3.0 and its diff is CHANGELOG + manifest only; then, and only then, the human merges it; release‑please tags `v0.3.0`; `release.yml` publishes; the README's `releases/latest/download` URL starts resolving.

### 13.3 The acceptance gate — commands whose output decides

Run on the P3 branch head, in this order. Every command must exit 0 and print the named line.

```
# 1. contract currency and the Go codec against the same bytes
python scripts/gen_debug_terminal_corpus.py --check            # "corpus current"
python scripts/gen_cli_contracts.py --check                    # "cli contracts current"
go test ./cli/internal/debugproto/ -run 'TestContract|TestEscape' -count=1 -v | grep -c -- '--- PASS'   # >= 40 (subtests are indented; an anchored grep counts only the 12 top-level tests)

# 2. the Go tier, gated, on the host and in the container
python scripts/run_tier.py TG                                  # "CI GATE [TG]: OK"
python scripts/run_tier.py T1                                  # "CI GATE [T1]: OK"  (Enforcement A green; corpus fresh)
python scripts/run_tier.py T3                                  # "CI GATE [T3]: OK"  (test_debug_rerun on cli/bin/lazyaf)

# 3. the ledger from both sides
uv run --directory backend pytest ../tdd/unit/scripts/test_cli_parity_ledger.py -q     # all passed, 0 skipped
go test ./cli/internal/parity/ -count=1                        # ok

# 4. the dogfood run (the real acceptance)
#    push the branch; the run must show tier1, tier-go, tier2, tier3 green and
#    "CI GATE [TG]: OK" in tier-go's log; verify-executor green.

# 5. the release path, without a tag
gh workflow run release.yml --ref <branch>                     # build + smoke(ubuntu,macos,windows) green; publish skipped
#    plus the recorded negative run: check_binary_version.py against a dist built with Version=9.9.9 exits 1

# 6. the owner, by hand, on Windows Git Bash and on one Linux box
curl -fsSL <artifact install.sh> | LAZYAF_VERSION=dev bash  # or: bash cli/install.sh --from-dir cli/dist
lazyaf --version                                               # "lazyaf dev+<sha> (...)"
lazyaf init && lazyaf doctor                                   # both exit 0 in a marked directory; neither prints a value
lazyaf list                                                    # names which server URL it used
lazyaf debug rerun <run> --break tier1 && lazyaf debug attach <sid> --token <t>
#    arrow keys edit the sidecar command line; Ctrl-] r resumes; the run completes.  Recorded in PLAN.md.
```

Any red line is fixed, never baselined; `tdd/qa` runs in no tier and is not part of the gate.

### 13.4 Tiers that must be green at the gate

| Tier | Floor | Measured | Constraint |
|---|---|---|---|
| T1 | 5595 | 5710 | Gains ~+30 during P0–P3 (currency tests, ledger, run_tier pin, check_binary_version). Floor untouched until the deletion commit, which LOWERS it with the §10.3 note. |
| TG | stamped P1 | — | Written only by the INTEGRATOR from a green container run; re‑stamped at P4. |
| T2 | 81 | 83 | Untouched by this wave. |
| T3 | 21 | 22 | **Margin of one.** The P2 subprocess switch adds process startup and pipe semantics to a test that already drives Docker, uvicorn and a real socket. In line mode the trailing `\n` after `\x1dr` becomes a stdin frame sent after `@resume`; the server may already be closing — harmless, and it must not be asserted against. A flake here is fixed in the test's timing, never skipped. |

### 13.5 The deletion list (R2: in the acceptance commit, not before)

```
cli/lazyaf/                              (all: __init__.py cli.py debug_cmd.py debug_protocol.py ...)
cli/pyproject.toml  cli/build/  cli/lazyaf_cli.egg-info/  cli/uv.lock (untracked)
scripts/bootstrap_secrets.py  scripts/preflight.py
tdd/unit/packaging/                      (whole directory)
tdd/unit/scripts/test_cli_debug.py  test_cli_errors.py  test_cli_tests_reconcile.py
tdd/unit/scripts/test_bootstrap_secrets.py  test_preflight.py
tdd/unit/scripts/test_cli_parity_ledger.py   (Enforcement A goes with the classes it counted)
.github/scripts/check_release_version.py
release.yml: jobs wheel, pypi; the publish_to_pypi input      pr-build.yml: job cli-wheel
release-please-config.json: the extra-files block             backend/pyproject.toml: rich (test extra)
tdd/unit/debug/test_terminal_protocol_contract.py: the six cross-import classes + the lazyaf import
```

`cli/LICENSE` stays (the binary is MIT too).

---

## 14. Docs to change, file:line (verified)

| File | Lines | Change |
|---|---|---|
| `README.md` | `:164-168` | `python scripts/bootstrap_secrets.py` / `preflight.py` → `lazyaf init` / `lazyaf doctor`; the "needs the docker SDK" note goes |
| | `:179` | `pip install ./cli` → the §6 install block |
| | `:254` | the two script names → the two commands |
| | `:389` | table row: `cli/` — "Go CLI (`lazyaf`): ingest, land, list, branches, tests reconcile, debug, init, doctor" |
| | `:477, :546` | "no CLI wheel to download" / "release.yml builds the CLI wheel" → binaries |
| `QUICKSTART.md` | `:36-39` | delete "Python 3.10+ for bootstrap_secrets.py"; Python is no longer a prerequisite for the release stack; the CLI is installed FIRST (the onboarding order flips — stated) |
| | `:61, :123-124, :129-130, :146, :244, :395, :448, :454, :462, :470, :474` | script names → `lazyaf init` / `lazyaf doctor`; `:448`'s quoted compose message becomes `not set. Run: lazyaf init` |
| | `:283-296` | `pip install ./cli` and the wheel paragraph → the §6 block; `:466` likewise |
| `CONTRIBUTING.md` | `:14` | historical sentence, keep; `:71` example scope only, keep; `:124-143` "the version lives in exactly one place" → the tag via release‑please's manifest; `check_binary_version.py` |
| `SECURITY.md` | wherever the wheel/`pip install` is named | binaries + `checksums.txt` verification |
| `.github/WORKFLOWS.md` | `:12, :28, :30, :47, :56, :73-74, :98, :252-260, :267-291, :346, :438, :456-457, :500, :528, :587-598, :616-618` | wheel/PyPI/`__init__.py`/`check_release_version.py` → binaries, `smoke` matrix, `check_binary_version.py`, `install.sh`, the "release PR diff is CHANGELOG + manifest" rule, the "do not merge the release PR before P4" warning, setup‑go in the pinned list |
| `docker-compose.release.yml` | `:7, :27-29, :97, :102-103, :164` | header and messages → `lazyaf init` / `lazyaf doctor` |
| `docker-compose.yml` | `:39-41, :107, :200-201, :241` | messages → `Run: lazyaf init` |
| `docker-compose.dev.yml` / `docker-compose.qa.yml` | `:17-18` / `:41, :45-46` | same |
| `backend/app/config.py` | `:133, :205` | remedy text → `lazyaf init` |
| `cli/README.md` | whole file | rewritten for the binary: install, build from source (Go ≥ 1.21 bootstraps the 1.26.8 toolchain download), `build_cli.sh`, the pytest‑collector caveat |
| `images/debug-sidecar/motd` | `:20, :28` | **unchanged** — subcommand names only, and they still exist |
| `PLAN.md` | decision log; Numbers table | the cutover decision, the T1 −N / TG +M reconciliation, the manual acceptance record; T1 count in the Numbers table |

---

## 15. Risks, and the things NOT to do

### 15.1 Risks, ranked by (silence × blast radius), each with the thing that catches it

1. **Windows raw‑console read granularity** (UNVERIFIED; §12). Catch: P1's first task is the spike on the owner's box; fallback named; worst case is a stated line mode.
2. **The T1 floor decrease lands in the same commit as a deletion** — the shape R4 distrusts most. Catch: the ledger maps every deleted class; Enforcement B keeps the carriers alive; TG's floor is stamped from a green run; the note reconciles per file from the last green junit. Reject the PR if the arithmetic does not close.
3. **T3 margin of one** with the subprocess switch. Catch: §13.4; timing fixes only, never a skip.
4. **The corpus pins encode/decode, not runtime bounds** (rate 200/s, queue 256, 64 KiB enforcement). Same coverage as today; T3's live run is the only cross‑check. The client's 48 KiB chunking and `SetReadLimit` pin are derived from the constant.
5. **gotestsum junit shape vs `ci_gate`** (skip `message` attribute; file written on compile failure). Catch: verified on the first P1 run; fallback is `go test -json` → junit inside `run_tier.py`.
6. **release‑please on a deleted extra‑file** (UNVERIFIED). Catch: config edit and file deletion in one commit; P4's re‑dispatch and diff inspection.
7. **The standing release PR is mergeable months before cutover.** No mechanical guard exists; hidden commit types keep it from becoming a `feat`, and the rule is written into WORKFLOWS.md.
8. **`releases/latest/download/install.sh` 404s until v0.3.0**; `latest` ignores prereleases. README wording is guarded; a prerelease needs `LAZYAF_VERSION` pinned.
9. **coder/websocket 32 KiB default read limit** — a silent 1009 on a legitimate 64 KiB stdout frame. Catch: `TestReadLimitIsTheContractBound`.
10. **`go.sum` and `scan_repo_secrets.py`**: base64 hashes could in principle match a live‑key pattern. The existing exact‑value allowlist is the fix, never a path exclusion.
11. **macOS bash 3.2 vs cobra's V2 completion** (UNVERIFIED). Catch: the macOS smoke sources it; red means document `bash-completion@2`, not weaken.
12. **The pytest collector keeps a Python dependency for one opt‑in mode.** Stated in `--help`, README and here; the interpreter chosen is named in the output.
13. **First `go build` on the go1.21.5 dev box downloads go1.26.8**; an offline box fails with Go's own clear error. Users never need Go.
14. **Go tarball download in every image build** — a go.dev outage reds `pr-build`/`images.yml`; same exposure class as the node/uv installs, larger in size.
15. **Reproducible builds** are limited to "`checksums.txt` is the CI reference"; `build_cli.sh` pins toolchain and flags, which is a property to state, not a guarantee to advertise.
16. **`test_debug_rerun.py` after cutover** needs `cli/bin/lazyaf` on the run volume from `tier-go`'s preflight; if a future reorder puts T3 before TG the test fails loudly naming `build_cli.sh` (never skips) — correct, but someone will be tempted to "fix" it with a skip.

### 15.2 What NOT to do

- Put `go.mod` under `cli/` — `go install ...@v0.3.0` would need `cli/v0.3.0` tags release‑please never creates, so `install.sh`'s source‑build fallback would lie.
- Let release‑please bump a Go source file via `extra-files` — a third copy of the version, and a source build past the release PR would claim a release version.
- Embed a second copy of the corpus in the Go module and pin the copies to each other — three sources of truth wearing a currency test.
- Use goreleaser — it owns tags, changelog and release notes that release‑please already owns; the matrix is a 12‑line loop.
- Add a Homebrew tap / scoop bucket / winget manifest — each needs a PAT; the CI's only credential is `GITHUB_TOKEN`.
- Document `go install` as THE install path — it needs a Go toolchain, the same class of burden as needing Python. It is the fallback for an unsupported arch, nothing more.
- Port the pytest collector to Go — it runs inside pytest by definition.
- Keep the Python CLI, `bootstrap_secrets.py` or `preflight.py` "as a fallback for one release" — two wire clients, two error idioms, two secret writers with their own placeholder rules; the ledger exists so the cutover can be hard.
- `t.Skip` anywhere in the Go suite — every "cannot run here" is `t.Fatal` with the reason.
- Add `go test` to `pr-build.yml` — GitHub packages, the dogfood pipeline gates.
- A `lazyaf self-update` verb — a second download‑and‑verify path to secure; re‑running the curl line is the upgrade.
- Edit rc files or run `setx` from `install.sh` — print the line; `setx` truncates PATH.
- A JSON output mode, a colour library, a TUI, `--break` completion, `--insecure` on `install.sh`, an `install.ps1`, attestations — real follow‑ups, none contract‑bearing, all scope creep on a change whose risk is already the floor arithmetic.
- Lower T1's floor without the per‑file reconciliation, or stamp TG's floor from anything but a green container run.
- Bake the Go binary into step or sidecar images — the CLI is a host tool; `images/debug-sidecar/motd` names subcommands only and stays as is.
- Silence dynamic completion's server failures without the written reason in `completion.go`.

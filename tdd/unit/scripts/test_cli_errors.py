"""What the `lazyaf` CLI says when something is wrong (cli/lazyaf/cli.py).

WHY THIS FILE EXISTS. The owner's report was "it fails if you don't ingest
with --name set and doesn't actually tell you". Reproducing it turned up one
defect with four faces, all of them the same thing: the CLI's error surface
could not be trusted to carry the fact that explained the failure.

  1. UNTRUSTED TEXT WENT THROUGH RICH'S MARKUP PARSER. rich reads `[word]` as
     a style tag: it DELETES it, and raises MarkupError on `[/word]`. Real,
     reproduced against the QA sandbox:

         git said   " ! [rejected]        master -> master (non-fast-forward)"
         CLI showed " !                   master -> master (non-fast-forward)"

     The single word naming the failure was the single word rich removed. A
     bracketed path in a server body ("no such file [/tmp/x]") went further
     and crashed the error report itself.

  2. THE SERVER'S WORDS WERE DISCARDED. `land`, `branches` and `list` printed
     "API returned 404" and dropped the body; `ingest` and `reconcile` dumped
     the raw pydantic envelope. Only the newest code (the debug verbs) quoted
     the server - the usual sign that an idiom was never centralised (R3).

  3. HALF THE httpx ERROR TREE WAS UNHANDLED. Only ConnectError and
     HTTPStatusError were caught, so a schemeless $LAZYAF_SERVER ended in a
     20-frame traceback ending in `httpx.UnsupportedProtocol` - a message
     about httpx's internals, naming neither the setting nor the fix.

  4. A FAILURE COULD EXIT 0. `ingest --all-branches` on a repo with no
     commits printed a green "Success!" and exited 0, having transferred
     nothing: `git push --all` on an unborn HEAD is a successful no-op. The
     real failure surfaced minutes later in the UI as the server's
     `_require_pushed_content` refusal. `land --pr` did the same on a smaller
     scale - PR creation failed, "Landed!" printed, exit 0.

`rich` is not installed in the backend test environment (the CLI ships its
own dependency set), so cli.py is imported behind a stub - the idiom
test_cli_tests_reconcile.py established, for the same reason (R4: the
refusals are genuinely exercised, not skipped). This file's stub RECORDS the
kwargs of every print, because "was this text handed to the markup parser?"
is precisely the contract under test. Where real rich IS importable, the last
class re-renders those recordings through it and proves end to end that
`[rejected]` survives.
"""
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_DIR = REPO_ROOT / "cli"

pytest.importorskip("click", reason="cli/ requires click")

if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import click  # noqa: E402
from click.testing import CliRunner  # noqa: E402

#: Every (text, kwargs) pair the CLI printed during a test.
PRINTS: list[dict] = []


def _rich_stub() -> dict:
    """A rich stand-in that records HOW each line was printed.

    Deliberately not just a printer. The bug this file exists for is a
    formatting flag, so the flag has to be observable.
    """

    class _Console:
        def __init__(self, *args, **kwargs):
            self.stderr = bool(kwargs.get("stderr"))

        def print(self, *args, **kwargs):
            text = " ".join(str(a) for a in args)
            PRINTS.append(
                {
                    "text": text,
                    "markup": kwargs.get("markup", True),
                    "stderr": self.stderr,
                }
            )
            # sys.stderr is looked up per call so that CliRunner's capture,
            # installed after import, is the stream that receives it.
            print(text, file=sys.stderr if self.stderr else sys.stdout)

    class _Panel(str):
        def __new__(cls, renderable="", *args, **kwargs):
            return super().__new__(cls, str(renderable))

        @classmethod
        def fit(cls, renderable="", *args, **kwargs):
            return cls(renderable)

    rich = types.ModuleType("rich")
    console_mod = types.ModuleType("rich.console")
    console_mod.Console = _Console
    panel_mod = types.ModuleType("rich.panel")
    panel_mod.Panel = _Panel
    rich.console = console_mod
    rich.panel = panel_mod
    return {"rich": rich, "rich.console": console_mod, "rich.panel": panel_mod}


_saved = {name: sys.modules.get(name) for name in ("rich", "rich.console", "rich.panel")}
sys.modules.update(_rich_stub())
sys.modules.pop("lazyaf.cli", None)
try:
    import lazyaf.cli as cli_module  # noqa: E402
    from lazyaf.cli import cli  # noqa: E402
finally:
    for _name, _mod in _saved.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


CLI_SOURCE = (CLI_DIR / "lazyaf" / "cli.py").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """The source of one top-level function in cli.py."""
    import ast

    tree = ast.parse(CLI_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(CLI_SOURCE, node) or ""
    raise AssertionError(f"cli.py no longer defines {name}()")


@pytest.fixture(autouse=True)
def _clear_prints():
    PRINTS.clear()
    yield
    PRINTS.clear()


@pytest.fixture
def run():
    runner = CliRunner()

    def _invoke(*args, **kwargs):
        return runner.invoke(cli, list(args), **kwargs)

    return _invoke


@pytest.fixture
def no_http(monkeypatch):
    """Fail loudly if a refusal path ever reaches the network.

    Every refusal in this file is one the CLI can make on its own. Reaching
    httpx would mean it created server-side state before deciding to refuse -
    which is exactly what `ingest --branch <typo>` used to do.
    """

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("this path must refuse BEFORE calling the API")

    monkeypatch.setattr(cli_module.httpx, "Client", _Boom)


def emitted() -> str:
    """Everything printed this test, in order."""
    return "\n".join(entry["text"] for entry in PRINTS)


# -----------------------------------------------------------------------------
# 1. Somebody else's words are reproduced exactly
# -----------------------------------------------------------------------------

#: The real thing, captured from `git push` against the QA sandbox.
GIT_REJECTION = (
    "To http://localhost:8790/git/54bf599d.git\n"
    " ! [rejected]        master -> master (non-fast-forward)\n"
    "error: failed to push some refs\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
)


class TestUntrustedTextIsNeverParsed:
    def test_git_stderr_is_quoted_verbatim_brackets_and_all(self, tmp_path):
        """The reported bug, at its source."""
        with pytest.raises(SystemExit):
            cli_module.fail("push failed", detail=GIT_REJECTION)

        text = emitted()
        assert "[rejected]" in text, (
            "the word naming the failure was dropped - this is the original "
            "defect: rich deleted `[rejected]` as if it were a style tag"
        )
        assert "(non-fast-forward)" in text

    def test_every_line_of_quoted_text_disables_markup(self):
        with pytest.raises(SystemExit):
            cli_module.fail("push failed", detail=GIT_REJECTION)

        quoted = [e for e in PRINTS if "rejected" in e["text"]]
        assert quoted, "the detail was not printed at all"
        for entry in quoted:
            assert entry["markup"] is False, (
                "untrusted text was handed to the markup parser; markup=False "
                "is the whole guard"
            )

    def test_a_closing_tag_shape_does_not_crash_the_report(self):
        """`[/tmp/x]` used to raise MarkupError while reporting a failure."""
        with pytest.raises(SystemExit):
            cli_module.fail("bad path", detail="no such file [/tmp/x]")
        assert "[/tmp/x]" in emitted()

    def test_the_summary_line_is_not_parsed_either(self):
        """Summaries interpolate argv and server values, so they are text too."""
        with pytest.raises(SystemExit):
            cli_module.fail("repo [weird] not found")

        summary = [e for e in PRINTS if "weird" in e["text"]]
        assert summary and all(e["markup"] is False for e in summary)
        assert "[weird]" in emitted()

    def test_the_remedy_is_not_parsed_either(self):
        with pytest.raises(SystemExit):
            cli_module.fail("nope", remedy="run: git log --format=[%h]")
        assert "[%h]" in emitted()

    def test_windows_line_endings_do_not_double_space_the_quote(self):
        with pytest.raises(SystemExit):
            cli_module.fail("x", detail="line one\r\nline two\r\n")
        quoted = [e["text"] for e in PRINTS if "line" in e["text"]]
        assert quoted == ["  line one", "  line two"]

    def test_an_empty_detail_is_not_printed_as_a_blank_quotation(self):
        with pytest.raises(SystemExit):
            cli_module.fail("x", detail="   \n\n")
        assert not [e for e in PRINTS if e["text"].strip() and "x" not in e["text"]]


# -----------------------------------------------------------------------------
# 2. A failure never exits 0
# -----------------------------------------------------------------------------


class TestNothingExitsZeroOnFailure:
    def test_fail_refuses_to_be_asked_for_a_zero_exit(self):
        """The guard is in the helper so no future caller can reintroduce it."""
        with pytest.raises(ValueError):
            cli_module.fail("boom", exit_code=0)

    @pytest.mark.parametrize("code", [1, 2])
    def test_fail_exits_with_the_code_it_was_given(self, code):
        with pytest.raises(SystemExit) as exc:
            cli_module.fail("boom", exit_code=code)
        assert exc.value.code == code

    def test_ingest_refuses_a_repo_with_no_commits(self, run, no_http, tmp_path):
        """`git push --all` on an unborn HEAD succeeds and sends nothing, so
        this used to print a green Success! and exit 0."""
        repo = tmp_path / "empty"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = run("ingest", str(repo), "--name", "x", "--all-branches")

        assert result.exit_code != 0
        assert "no commits" in emitted()

    def test_the_no_commits_refusal_names_the_two_commands_that_fix_it(
        self, run, no_http, tmp_path
    ):
        repo = tmp_path / "empty"
        repo.mkdir()
        (repo / ".git").mkdir()

        run("ingest", str(repo), "--name", "x")

        text = emitted()
        assert "git add -A" in text
        assert "git commit" in text


# -----------------------------------------------------------------------------
# 3. A missing argument shows an invocation that works
# -----------------------------------------------------------------------------


class TestUsageErrorsCarryAnExample:
    def test_ingest_without_name_names_the_flag_and_shows_a_command(self, run):
        """The owner's exact report."""
        result = run("ingest", ".")

        assert result.exit_code != 0
        assert "--name" in result.output
        assert "lazyaf ingest ./my-project --name my-project" in result.output, (
            "click named the missing flag and pointed at --help; naming the "
            "flag is not the same as showing the command that works"
        )

    def test_land_without_branch_shows_a_command(self, run):
        result = run("land", "abc123")
        assert result.exit_code != 0
        assert "--branch" in result.output
        assert "lazyaf land abc123 --branch feature/new-api" in result.output

    def test_a_missing_positional_shows_a_command_too(self, run):
        result = run("branches")
        assert result.exit_code != 0
        assert "lazyaf branches abc123" in result.output

    def test_the_example_comes_from_the_commands_own_help(self):
        """R3: one source. The example after a mistake is the example in
        --help, so it cannot rot on the path nobody reads."""
        ingest = cli.commands["ingest"]
        assert cli_module.command_examples(ingest) == [
            line.strip()
            for line in ingest.help.splitlines()
            if line.strip().startswith("lazyaf ingest")
        ]

    @pytest.mark.parametrize(
        "path",
        [
            ("ingest",),
            ("land",),
            ("branches",),
            ("list",),
            ("tests", "reconcile"),
            ("debug", "rerun"),
            ("debug", "list"),
            ("debug", "status"),
            ("debug", "attach"),
            ("debug", "resume"),
            ("debug", "abort"),
            ("debug", "extend"),
        ],
        ids=lambda p: " ".join(p),
    )
    def test_every_command_documents_a_runnable_example(self, path):
        """The mechanism above can only show what the docstrings carry."""
        command = cli
        for name in path:
            command = command.commands[name]
        examples = cli_module.command_examples(command)
        assert examples, f"`lazyaf {' '.join(path)}` documents no example"
        for line in examples:
            assert line.startswith("lazyaf "), (
                f"example is not a runnable command line: {line!r}"
            )

    def test_a_mistyped_command_suggests_the_real_one(self, run):
        result = run("ingets", ".")
        assert result.exit_code != 0
        assert "Did you mean 'ingest'?" in result.output

    def test_a_wholly_unknown_command_does_not_invent_a_suggestion(self, run):
        result = run("zzzzzzzz")
        assert result.exit_code != 0
        assert "Did you mean" not in result.output


# -----------------------------------------------------------------------------
# 4. The backend URL: named, validated, never guessed
# -----------------------------------------------------------------------------


class TestBackendUrl:
    def test_a_schemeless_url_is_refused_not_guessed(self, monkeypatch):
        """Was: a traceback ending in httpx.UnsupportedProtocol."""
        monkeypatch.setenv("LAZYAF_SERVER", "localhost:8790")
        with pytest.raises(SystemExit) as exc:
            cli_module.resolve_server_url(None)

        assert exc.value.code != 0
        text = emitted()
        assert "scheme" in text
        assert "http://localhost:8790" in text, "the refusal must show the fix"

    def test_the_schemeless_refusal_says_why_guessing_is_wrong(self, monkeypatch):
        monkeypatch.setenv("LAZYAF_SERVER", "localhost:8790")
        with pytest.raises(SystemExit):
            cli_module.resolve_server_url(None)
        assert "clear" in emitted(), "guessing http:// downgrades an https URL"

    def test_it_matches_the_rule_the_terminal_client_already_applies(self):
        """R3: `debug_cmd.terminal_url` has refused schemeless URLs since
        12.7. The HTTP path guessed nothing either - it just crashed."""
        assert "no http(s)://" in CLI_DIR.joinpath(
            "lazyaf", "debug_cmd.py"
        ).read_text(encoding="utf-8")

    def test_an_empty_server_setting_is_refused(self, monkeypatch):
        monkeypatch.setenv("LAZYAF_SERVER", "")
        with pytest.raises(SystemExit):
            cli_module.resolve_server_url(None)
        assert "empty" in emitted()

    def test_a_trailing_slash_does_not_become_a_double_slash_404(self, monkeypatch):
        """`LAZYAF_SERVER=http://h:8790/` produced `//api/repos` -> a bare
        404 that blamed the server."""
        monkeypatch.setenv("LAZYAF_SERVER", "http://h:8790/")
        assert cli_module.resolve_server_url(None) == "http://h:8790"

    @pytest.mark.parametrize(
        "env,explicit,expected",
        [
            (None, "http://x", "--server"),
            ("http://y", None, "$LAZYAF_SERVER"),
            (None, None, "the built-in default"),
        ],
    )
    def test_the_message_says_where_the_url_came_from(
        self, monkeypatch, env, explicit, expected
    ):
        """"Could not connect" is often "connected to the wrong thing"; the
        URL alone does not settle which."""
        monkeypatch.delenv("LAZYAF_SERVER", raising=False)
        if env:
            monkeypatch.setenv("LAZYAF_SERVER", env)
        assert expected in cli_module.describe_server(explicit)

    def test_describe_server_names_both_ways_to_change_it(self, monkeypatch):
        monkeypatch.delenv("LAZYAF_SERVER", raising=False)
        described = cli_module.describe_server(None)
        assert "--server" in described
        assert "LAZYAF_SERVER" in described


# -----------------------------------------------------------------------------
# 5. The server's own words reach the operator
# -----------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload) if payload else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cli_module.httpx.HTTPStatusError(
                "boom", request=None, response=self
            )


def _client_returning(response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, **kwargs):
            return response

    return _Client


def _client_raising(exc):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, **kwargs):
            raise exc

    return _Client


class TestTheServerIsQuoted:
    def test_a_string_detail_is_printed(self, monkeypatch):
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_returning(
                _Response(400, {"detail": "Repo 'x' has no commits yet"})
            ),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("GET", "/api/repos", "http://h")
        assert "Repo 'x' has no commits yet" in emitted()

    def test_a_pydantic_422_envelope_is_rendered_readably(self, monkeypatch):
        """Was dumped raw: {"detail":[{"type":"string_too_short","loc":...}]}"""
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_returning(
                _Response(
                    422,
                    {
                        "detail": [
                            {
                                "type": "string_too_short",
                                "loc": ["body", "name"],
                                "msg": "String should have at least 1 character",
                            }
                        ]
                    },
                )
            ),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("POST", "/api/repos/ingest", "http://h")

        text = emitted()
        assert "name: String should have at least 1 character" in text
        assert "string_too_short" not in text, "the envelope leaked through"

    def test_a_non_json_error_body_is_still_shown(self, monkeypatch):
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_returning(_Response(502, None, text="<h1>Bad Gateway</h1>")),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("GET", "/api/repos", "http://h")
        assert "Bad Gateway" in emitted()

    def test_a_status_with_no_body_says_so_rather_than_going_quiet(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            cli_module.httpx, "Client", _client_returning(_Response(500, None))
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("GET", "/api/repos", "http://h")
        text = emitted()
        assert "500" in text
        assert "no explanation" in text

    def test_a_404_uses_the_commands_own_words_when_it_has_them(self, monkeypatch):
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_returning(_Response(404, {"detail": "Repo not found"})),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request(
                "GET",
                "/api/repos/abc/branches",
                "http://h",
                not_found="repo abc does not exist. `lazyaf list` shows the ids that do.",
            )
        text = emitted()
        assert "`lazyaf list` shows the ids" in text
        assert "Repo not found" in text, "the server's own line is still quoted"

    def test_a_200_that_is_not_json_blames_the_right_component(self, monkeypatch):
        """Pointing at a dev server or a proxy error page is a setup mistake,
        not 'invalid JSON'."""
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_returning(_Response(200, None, text="<!doctype html>")),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("GET", "/api/repos", "http://h")
        assert "not the LazyAF API" in emitted()


class TestEveryTransportFailureIsHandled:
    """Only ConnectError and HTTPStatusError used to be caught; the rest of
    the tree reached the terminal as a traceback."""

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(
                lambda h: h.ConnectError("refused"), id="ConnectError"
            ),
            pytest.param(
                lambda h: h.ConnectTimeout("slow"), id="ConnectTimeout"
            ),
            pytest.param(lambda h: h.ReadTimeout("slow"), id="ReadTimeout"),
            pytest.param(lambda h: h.PoolTimeout("slow"), id="PoolTimeout"),
            pytest.param(
                lambda h: h.RemoteProtocolError("truncated"),
                id="RemoteProtocolError",
            ),
            pytest.param(lambda h: h.ProxyError("proxy"), id="ProxyError"),
            pytest.param(
                lambda h: h.UnsupportedProtocol("no scheme"),
                id="UnsupportedProtocol",
            ),
            pytest.param(lambda h: h.ReadError("reset"), id="ReadError"),
            pytest.param(
                lambda h: h.TooManyRedirects("loop"), id="TooManyRedirects"
            ),
        ],
    )
    def test_it_becomes_a_refusal_not_a_traceback(self, monkeypatch, exc):
        monkeypatch.setattr(
            cli_module.httpx, "Client", _client_raising(exc(cli_module.httpx))
        )
        with pytest.raises(SystemExit) as raised:
            cli_module.api_request("GET", "/api/repos", "http://h:1")
        assert raised.value.code != 0
        assert "Error:" in emitted()

    def test_an_unreachable_backend_names_the_url_and_how_to_check_it(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_raising(cli_module.httpx.ConnectError("refused")),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("GET", "/api/repos", "http://h:9999")

        text = emitted()
        assert "http://h:9999" in text
        assert "curl http://h:9999/health" in text

    def test_a_timeout_is_distinguished_from_a_refusal(self, monkeypatch):
        """"Not answering" and "not listening" have different causes; one
        message for both sends the reader to the wrong place."""
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_raising(cli_module.httpx.ReadTimeout("slow")),
        )
        with pytest.raises(SystemExit):
            cli_module.api_request("GET", "/api/repos", "http://h")
        assert "did not answer" in emitted()


# -----------------------------------------------------------------------------
# 6. One implementation, not five (R3)
# -----------------------------------------------------------------------------


class TestOneErrorIdiom:
    def test_only_api_request_talks_to_httpx(self):
        """Five copies of a try/except is how three of them ended up wrong."""
        assert CLI_SOURCE.count("httpx.Client(") == 1, (
            "a command is building its own HTTP client again; route it "
            "through api_request so it inherits the error idiom"
        )

    def test_every_httpx_handler_lives_inside_api_request(self):
        """Handlers outside it are the copies that drifted: three commands
        dropped the response body, two dumped the raw envelope."""
        body = _function_source("api_request")
        for pattern in ("except httpx.HTTPStatusError", "except httpx.RequestError"):
            assert CLI_SOURCE.count(pattern) == 1, (
                f"{pattern!r} appears more than once; a second handler will "
                "drift from api_request's"
            )
            assert pattern in body, f"{pattern!r} moved out of api_request"

    def test_no_command_catches_connecterror_on_its_own(self):
        """The old idiom, gone: `except httpx.ConnectError` in five places,
        each with its own wording and its own omissions."""
        assert "except httpx.ConnectError" not in CLI_SOURCE

    def test_no_command_prints_a_bare_status_code_any_more(self):
        assert "API returned {e.response.status_code}" not in CLI_SOURCE
        assert "console.print(e.response.text)" not in CLI_SOURCE

    def test_the_debug_wrapper_delegates_rather_than_duplicating(self):
        """`debug_cmd` imports `_debug_request`, so the name stays - but it
        must not carry a second implementation."""
        assert "return api_request(method, path, server, **kwargs)" in CLI_SOURCE

    def test_debug_cmd_still_imports_what_it_expects(self):
        source = (CLI_DIR / "lazyaf" / "debug_cmd.py").read_text(encoding="utf-8")
        for name in ("_debug_request", "console", "get_server_url"):
            assert name in source
            assert hasattr(cli_module, name), (
                f"debug_cmd imports {name} from lazyaf.cli; renaming it breaks "
                "the interactive terminal at runtime, not at import"
            )


# -----------------------------------------------------------------------------
# 7. Diagnostics on stderr, results on stdout
# -----------------------------------------------------------------------------


class TestStreams:
    def test_refusals_go_to_stderr(self):
        with pytest.raises(SystemExit):
            cli_module.fail("nope", detail="because", remedy="do this")
        assert PRINTS and all(entry["stderr"] for entry in PRINTS)

    def test_warnings_go_to_stderr(self):
        cli_module.warn("heads up")
        assert PRINTS and all(entry["stderr"] for entry in PRINTS)

    def test_results_stay_on_stdout(self, monkeypatch):
        """A refusal on stderr is only useful if `lazyaf list | ...` is still
        clean data."""
        monkeypatch.setattr(
            cli_module.httpx,
            "Client",
            _client_returning(
                _Response(
                    200,
                    [
                        {
                            "id": "abc",
                            "name": "demo",
                            "is_ingested": True,
                            "remote_url": None,
                        }
                    ],
                )
            ),
        )
        CliRunner().invoke(cli, ["list", "--server", "http://h"])
        assert any(
            "demo" in entry["text"] and not entry["stderr"] for entry in PRINTS
        )


# -----------------------------------------------------------------------------
# 8. ingest refuses before it creates server-side state
# -----------------------------------------------------------------------------


def _git_repo(tmp_path, branches=("main",)):
    """A directory shaped like a git repo, with `run_git` stubbed to match."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


class TestIngestRefusesEarly:
    @pytest.fixture
    def branches(self, monkeypatch):
        """Control what `git for-each-ref` reports."""
        state = {"names": ["main", "dev"]}

        def _run_git(args, cwd=None):
            class _Result:
                returncode = 0
                stdout = "\n".join(state["names"])
                stderr = ""

            return _Result()

        monkeypatch.setattr(cli_module, "run_git", _run_git)
        return state

    def test_a_blank_name_is_refused_here_not_by_the_api(
        self, run, no_http, tmp_path, branches
    ):
        """`--name "   "` got past click and came back as a raw 422."""
        repo = _git_repo(tmp_path)
        result = run("ingest", str(repo), "--name", "   ")

        assert result.exit_code != 0
        text = emitted()
        assert "--name is empty" in text
        assert "lazyaf ingest" in text, "the refusal shows a working command"

    def test_a_nonexistent_branch_is_refused_before_the_repo_is_created(
        self, run, no_http, tmp_path, branches
    ):
        """This used to create a repo record, then fail on push - leaving an
        empty repo in LazyAF for every typo."""
        repo = _git_repo(tmp_path)
        result = run("ingest", str(repo), "--name", "x", "--branch", "mian")

        assert result.exit_code != 0
        assert "does not exist" in emitted()

    def test_the_branch_refusal_lists_the_branches_that_do_exist(
        self, run, no_http, tmp_path, branches
    ):
        repo = _git_repo(tmp_path)
        run("ingest", str(repo), "--name", "x", "--branch", "mian")

        text = emitted()
        assert "main" in text
        assert "dev" in text

    def test_a_path_that_is_not_a_git_repo_names_the_remedy(
        self, run, no_http, tmp_path
    ):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = run("ingest", str(plain), "--name", "x")

        assert result.exit_code != 0
        text = emitted()
        assert "not a git repository" in text
        assert "git init" in text


# -----------------------------------------------------------------------------
# 9. land: the branch result and the PR result are reported separately
# -----------------------------------------------------------------------------


class TestLand:
    @pytest.fixture
    def landing(self, monkeypatch, tmp_path):
        """A `land` that gets as far as pushing, with every seam recorded."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir()

        calls = {"git": [], "gh": []}

        def _api(method, path, server, **kwargs):
            if path.endswith("/clone-url"):
                return {"clone_url": "http://h/git/abc.git"}
            return {"remote_url": "git@github.com:o/r.git", "default_branch": "main"}

        def _run_git(args, cwd=None):
            calls["git"].append(args)

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        monkeypatch.setattr(cli_module, "api_request", _api)
        monkeypatch.setattr(cli_module, "run_git", _run_git)
        return calls

    def test_the_push_refspec_is_fully_qualified_on_both_sides(self, run, landing):
        """`lazyaf/<branch>:<branch>` fails whenever the destination branch
        does not exist yet - i.e. every time an agent branch is landed for
        the first time. git: "Neither worked, so we gave up. You must fully
        qualify the ref." """
        run("land", "abc", "--branch", "agent/fix", "--remote", "origin")

        pushes = [args for args in landing["git"] if args[:1] == ["push"]]
        assert pushes == [
            [
                "push",
                "origin",
                "refs/remotes/lazyaf/agent/fix:refs/heads/agent/fix",
            ]
        ]

    def test_a_failed_pr_does_not_report_success(self, run, landing, monkeypatch):
        """Was: "Warning: PR creation failed", then a green "Landed!", exit 0
        - a script above it saw the whole request honoured."""

        class _Gh:
            returncode = 1
            stdout = ""
            stderr = "no GitHub host configured"

        monkeypatch.setattr(
            cli_module.subprocess, "run", lambda *a, **k: _Gh()
        )

        result = run("land", "abc", "--branch", "agent/fix", "--pr")

        assert result.exit_code != 0
        text = emitted()
        assert "no GitHub host configured" in text, "gh's own words"
        assert "was pushed to" in text, "the half that DID work is still stated"
        assert "does not need repeating" in text
        assert "gh pr create" in text

    def test_a_successful_pr_still_reports_success(self, run, landing, monkeypatch):
        class _Gh:
            returncode = 0
            stdout = "https://github.com/o/r/pull/7"
            stderr = ""

        monkeypatch.setattr(
            cli_module.subprocess, "run", lambda *a, **k: _Gh()
        )

        result = run("land", "abc", "--branch", "agent/fix", "--pr")

        assert result.exit_code == 0
        assert "https://github.com/o/r/pull/7" in emitted()

    def test_running_outside_a_clone_names_the_remedy(self, run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no .git here

        def _api(method, path, server, **kwargs):
            if path.endswith("/clone-url"):
                return {"clone_url": "http://h/git/abc.git"}
            return {"remote_url": None, "default_branch": "main"}

        monkeypatch.setattr(cli_module, "api_request", _api)

        result = run("land", "abc", "--branch", "agent/fix")

        assert result.exit_code != 0
        text = emitted()
        assert "not a git repository" in text
        assert "cd /path/to/your/clone" in text


# -----------------------------------------------------------------------------
# 10. Proof against the real library, where it is installed
# -----------------------------------------------------------------------------


class TestAgainstRealRich:
    """The stub above pins the CALL (markup=False). This pins the EFFECT.

    These used to `importorskip` their way out of the backend test
    environment, on the reasoning that the CLI ships its own dependency set.
    But every tier runs pytest from `backend/`, so "skips here, runs where the
    CLI runs" meant runs NOWHERE - four tests that existed only to be skipped,
    and the gate said so. `rich` is now in backend's `test` extra, pinned to
    the same bound cli/pyproject.toml uses, so the effect is pinned against
    the library that actually ships (R4).
    """

    def _render(self, text, **kwargs):
        rich_console = pytest.importorskip("rich.console")
        import io

        buffer = io.StringIO()
        console = rich_console.Console(
            file=buffer, width=200, no_color=True, highlight=False
        )
        console.print(text, **kwargs)
        return buffer.getvalue()

    def test_markup_on_really_does_delete_the_word(self):
        """The bug, demonstrated - so the next reader knows why the flag is
        there and does not 'tidy' it away."""
        rendered = self._render(" ! [rejected]  main -> main")
        assert "[rejected]" not in rendered

    def test_markup_off_keeps_it(self):
        rendered = self._render(" ! [rejected]  main -> main", markup=False)
        assert "[rejected]" in rendered

    def test_a_closing_tag_shape_raises_with_markup_on(self):
        pytest.importorskip("rich.errors")
        from rich.errors import MarkupError

        with pytest.raises(MarkupError):
            self._render("no such file [/tmp/x]")

    def test_and_is_harmless_with_markup_off(self):
        assert "[/tmp/x]" in self._render("no such file [/tmp/x]", markup=False)

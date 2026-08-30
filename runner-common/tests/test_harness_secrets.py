"""
Endpoint auth never leaks (Milestone 14, risk register row 4).

The failure being prevented: a 401 body echoes the key, the harness logs it,
``StepRun.logs`` carries it to the UI, and the operator's LAN model key is now
in the database. Every path an upstream string can take to a log line or a
child process is checked here against a planted sentinel.

Shape borrowed from 12.6's ``test_secret_hygiene.py``: plant one value, then
grep EVERYTHING the step emitted.
"""
import json
import os

import pytest

from runner_common.harness.client import (
    REDACTED,
    OpenAICompatClient,
    auth_headers,
    scrub_secrets,
)
from runner_common.harness.constants import EXIT_ENDPOINT
from runner_common.harness.tools import Sandbox, run_tool
from tests.fixtures.openai import (
    DEFAULT_USAGE_SERIES,
    API_KEY_ENV,
    SENTINEL_KEY,
    FakeResponse,
    FakeSession,
    chat_response,
    endpoint_block,
    harness_block,
    make_repo,
    run_harness,
    tool_call,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def authed_endpoint(**overrides):
    return endpoint_block(auth_style="bearer", auth_env=API_KEY_ENV, **overrides)


def child_env():
    return {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
        API_KEY_ENV: SENTINEL_KEY,
        "LAZYAF_PIPELINE_RUN_ID": "p1",
        "LAZYAF_STEP_RUN_ID": "s1",
    }


# --------------------------------------------------------------------------
# the scrubber
# --------------------------------------------------------------------------

class TestScrubber:
    def test_it_removes_the_known_value(self):
        assert scrub_secrets(f"401: bad key {SENTINEL_KEY}", [SENTINEL_KEY]) == (
            f"401: bad key {REDACTED}"
        )

    def test_it_removes_the_bearer_form_even_when_the_value_is_unknown(self):
        assert scrub_secrets("Authorization: Bearer abc123xyz") == (
            f"Authorization: Bearer {REDACTED}"
        )

    def test_it_removes_the_sk_shape(self):
        assert REDACTED in scrub_secrets("your key sk-ABCdef0123456789 is invalid")

    def test_it_never_raises(self):
        class Explodes:
            def __str__(self):
                raise RuntimeError("nope")

        assert scrub_secrets(Explodes()) == REDACTED

    def test_a_short_value_is_not_used_as_a_replacement_pattern(self):
        """Replacing a 2-character 'secret' everywhere would destroy the
        message it was supposed to protect."""
        assert scrub_secrets("the model is qwen", ["en"]) == "the model is qwen"


# --------------------------------------------------------------------------
# headers
# --------------------------------------------------------------------------

class TestAuthHeaders:
    def test_none_is_a_first_class_case_with_no_header_at_all(self):
        assert auth_headers("none", None, None) == {}
        assert auth_headers("none", SENTINEL_KEY, None) == {}

    def test_bearer(self):
        assert auth_headers("bearer", SENTINEL_KEY, None) == {
            "Authorization": f"Bearer {SENTINEL_KEY}"
        }

    def test_a_named_header(self):
        assert auth_headers("header", SENTINEL_KEY, "x-api-key") == {
            "x-api-key": SENTINEL_KEY
        }

    def test_the_key_reaches_the_request_but_nothing_else(self, repo):
        result, logs, session, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[tool_call("finish", {"status": "blocked", "summary": "x"})],
                    usage=dict(DEFAULT_USAGE_SERIES[0]),
                )
            ],
            endpoint=authed_endpoint(),
            env=child_env(),
        )
        assert session.sent_headers[0]["Authorization"] == f"Bearer {SENTINEL_KEY}"
        assert SENTINEL_KEY not in "\n".join(logs)


# --------------------------------------------------------------------------
# the sentinel sweep
# --------------------------------------------------------------------------

class TestSentinelSweep:
    def test_a_401_body_echoing_the_key_never_reaches_a_log_line(self, repo):
        """The specific failure this design calls out by name."""
        echo = FakeResponse(
            401, text=json.dumps({"error": f"invalid api key: {SENTINEL_KEY}"})
        )
        result, logs, _, _ = run_harness(
            repo, [echo], endpoint=authed_endpoint(), env=child_env()
        )
        assert result.exit_code == EXIT_ENDPOINT
        joined = "\n".join(logs) + "\n" + json.dumps(result.usage) + "\n" + (result.error or "")
        assert SENTINEL_KEY not in joined
        assert REDACTED in result.error

    def test_the_key_is_in_no_emitted_line_of_a_whole_step(self, repo):
        script = [
            chat_response(
                tool_calls=[tool_call("write_file", {"path": "d.txt", "content": "x"})],
                usage=dict(DEFAULT_USAGE_SERIES[0]),
            ),
            chat_response(
                content=f"my key is {SENTINEL_KEY} by the way",
                tool_calls=[tool_call("finish", {"status": "success", "summary": "ok"})],
                usage=dict(DEFAULT_USAGE_SERIES[1]),
            ),
        ]
        result, logs, _, _ = run_harness(
            repo, script, endpoint=authed_endpoint(), env=child_env()
        )
        assert result.success is True
        for line in logs:
            assert SENTINEL_KEY not in line, f"leaked in: {line}"
        # Even the MODEL's own prose is scrubbed on the way out.
        assert any(REDACTED in line for line in logs)

    def test_the_shell_child_never_sees_the_key(self, repo):
        sandbox = Sandbox(
            workdir=repo,
            api_key_env=API_KEY_ENV,
            api_key_value=SENTINEL_KEY,
            base_env=child_env(),
        )
        result = run_tool(sandbox, "run_shell", {"command": "env || set"})
        payload = json.loads(result.text)
        assert SENTINEL_KEY not in payload["stdout"]
        assert SENTINEL_KEY not in payload["stderr"]

    def test_the_key_is_in_no_transcript_message(self, repo):
        _, _, session, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[tool_call("finish", {"status": "blocked", "summary": "x"})],
                    usage=dict(DEFAULT_USAGE_SERIES[0]),
                )
            ],
            endpoint=authed_endpoint(),
            env=child_env(),
        )
        assert SENTINEL_KEY not in json.dumps(session.bodies)


# --------------------------------------------------------------------------
# the refusal names the variable, never the value
# --------------------------------------------------------------------------

class TestMissingKeyRefusal:
    def test_an_unset_variable_fails_before_any_request_and_names_it(self, repo):
        result, logs, session, _ = run_harness(
            repo,
            [chat_response(content="never reached")],
            endpoint=authed_endpoint(),
            env={"PATH": os.environ.get("PATH", "")},
        )
        assert result.exit_code == EXIT_ENDPOINT
        assert API_KEY_ENV in result.error
        assert "unset or empty" in result.error
        assert session.requests == [], (
            "burning 30s of container start to reach an opaque 401 is exactly "
            "what this refusal prevents"
        )

    def test_an_auth_style_with_no_named_variable_is_refused(self, repo):
        result, _, session, _ = run_harness(
            repo,
            [chat_response(content="never reached")],
            endpoint=endpoint_block(auth_style="bearer", auth_env=None),
            env=child_env(),
        )
        assert result.exit_code == EXIT_ENDPOINT
        assert "endpoint.auth_env" in result.error
        assert session.requests == []

    def test_auth_style_none_needs_no_variable_and_still_runs(self, repo):
        result, _, session, _ = run_harness(
            repo,
            [
                chat_response(
                    tool_calls=[tool_call("finish", {"status": "blocked", "summary": "x"})],
                    usage=dict(DEFAULT_USAGE_SERIES[0]),
                )
            ],
            env={"PATH": os.environ.get("PATH", "")},
        )
        assert len(session.requests) == 1
        assert "Authorization" not in session.sent_headers[0]


# --------------------------------------------------------------------------
# the client's own scrubbing seam
# --------------------------------------------------------------------------

def test_the_client_scrubs_with_its_own_key():
    client = OpenAICompatClient(
        base_url="http://x/v1", model="m", api_key=SENTINEL_KEY, session=object()
    )
    assert client.scrub(f"boom {SENTINEL_KEY}") == f"boom {REDACTED}"

"""Config surface - Phase 12.6, section 4.5.

Not in the numbered test contract, but the two settings below are where a
remote deployment actually goes wrong, so they get the same treatment:

* a runner id that is not stable across restarts orphans a registry row every
  time the process bounces (failure_01 minted a fresh uuid4 per process);
* ``LAZYAF_STEP_BACKEND_URL`` / ``LAZYAF_GIT_URL_TEMPLATE`` are the answer to
  "the step container cannot reach the backend", which is the single most
  likely remote deployment failure (section 3.4).
"""
from __future__ import annotations

import platform
import socket

import pytest

from lazyaf_runner.cli import build_parser, config_from_args
from lazyaf_runner.config import (
    DEFAULT_RUNNER_TOKEN,
    ConfigError,
    RunnerConfig,
    is_loopback,
    parse_labels,
    websocket_url,
)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def test_parse_labels_basic() -> None:
    assert parse_labels("arch=arm64,zone=workshop") == {
        "arch": "arm64",
        "zone": "workshop",
    }


def test_parse_labels_repeated_key_becomes_a_list() -> None:
    """The only way a flat env string can express `has: [gpio, camera]`."""
    assert parse_labels("has=gpio,has=camera,has=lidar")["has"] == [
        "gpio",
        "camera",
        "lidar",
    ]


def test_parse_labels_bare_token_is_true_not_dropped() -> None:
    assert parse_labels("gpu") == {"gpu": "true"}


def test_parse_labels_tolerates_whitespace_and_empties() -> None:
    assert parse_labels(" a = 1 , , b=2 ,") == {"a": "1", "b": "2"}
    assert parse_labels("") == {}
    assert parse_labels(None) == {}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_runner_id_defaults_to_hostname_and_orchestrator_and_is_stable() -> None:
    first = RunnerConfig(orchestrator="docker")
    second = RunnerConfig(orchestrator="docker")
    assert first.runner_id == f"{socket.gethostname()}-docker"
    assert first.runner_id == second.runner_id, (
        "a runner id that changes per process orphans a registry row on every "
        "restart"
    )


def test_name_defaults_to_the_runner_id() -> None:
    config = RunnerConfig(runner_id="pi-workshop-1")
    assert config.name == "pi-workshop-1"


def test_arch_is_always_reported_raw_and_never_configured() -> None:
    """One alias table, backend-side (cross-agent contract #5). Two
    normalizers is how arm64 stops matching aarch64."""
    config = RunnerConfig(labels={"arch": "definitely-wrong", "zone": "w"})
    assert config.labels["arch"] == platform.machine()
    assert config.labels["zone"] == "w"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "backend,expected",
    [
        ("http://localhost:8000", "ws://localhost:8000/ws/runner"),
        ("https://lazyaf.example.com", "wss://lazyaf.example.com/ws/runner"),
        ("http://10.0.0.5:8000/", "ws://10.0.0.5:8000/ws/runner"),
        ("https://example.com/lazyaf", "wss://example.com/lazyaf/ws/runner"),
        ("wss://example.com", "wss://example.com/ws/runner"),
    ],
)
def test_websocket_url(backend, expected) -> None:
    assert websocket_url(backend) == expected


def test_websocket_url_rejects_a_scheme_it_cannot_speak() -> None:
    with pytest.raises(ConfigError):
        websocket_url("ftp://example.com")
    with pytest.raises(ConfigError):
        websocket_url("localhost:8000")  # no scheme -> no netloc


@pytest.mark.parametrize("url", ["ws://localhost:1", "ws://127.0.0.1:8000", "ws://[::1]:80"])
def test_loopback_detection(url) -> None:
    assert is_loopback(url)


def test_non_loopback_is_not_loopback() -> None:
    assert not is_loopback("ws://10.0.0.5:8000")


# ---------------------------------------------------------------------------
# The plaintext guard
# ---------------------------------------------------------------------------

def test_plaintext_to_loopback_is_allowed() -> None:
    RunnerConfig(backend_url="http://localhost:8000").validate()


def test_plaintext_to_a_real_host_is_refused_by_default() -> None:
    """execute_step carries the step JWT and secret_environment inside
    control_files; in the clear across a real network that is a credential
    broadcast."""
    with pytest.raises(ConfigError) as excinfo:
        RunnerConfig(backend_url="http://10.0.0.5:8000").validate()
    assert "step JWT" in str(excinfo.value)


def test_plaintext_to_a_real_host_can_be_opted_into() -> None:
    RunnerConfig(backend_url="http://10.0.0.5:8000", allow_insecure=True).validate()


def test_tls_to_a_real_host_needs_no_opt_in() -> None:
    RunnerConfig(backend_url="https://lazyaf.example.com").validate()


# ---------------------------------------------------------------------------
# Remote URL overrides (section 3.4)
# ---------------------------------------------------------------------------

def test_step_backend_url_override() -> None:
    config = RunnerConfig(step_backend_url="http://10.0.0.5:8000")
    assert config.resolve_backend_url("http://backend:8000") == "http://10.0.0.5:8000"


def test_step_backend_url_falls_through_when_unset() -> None:
    assert RunnerConfig().resolve_backend_url("http://backend:8000") == "http://backend:8000"


def test_git_url_template_override_substitutes_repo_id() -> None:
    config = RunnerConfig(git_url_template="https://git.example.com/{repo_id}.git")
    assert (
        config.resolve_clone_url("http://backend:8000/git/r9f8.git", "r9f8")
        == "https://git.example.com/r9f8.git"
    )


def test_git_url_falls_through_when_unset() -> None:
    original = "http://backend:8000/git/r9f8.git"
    assert RunnerConfig().resolve_clone_url(original, "r9f8") == original


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------

def test_from_env_reads_every_documented_variable() -> None:
    config = RunnerConfig.from_env(
        {
            "LAZYAF_BACKEND_URL": "https://lazyaf.example.com",
            "LAZYAF_RUNNER_ID": "pi-workshop-1",
            "LAZYAF_RUNNER_NAME": "Workshop Pi",
            "LAZYAF_RUNNER_TYPE": "generic",
            "LAZYAF_RUNNER_LABELS": "has=gpio,has=camera,zone=workshop",
            "LAZYAF_ORCHESTRATOR": "docker",
            "LAZYAF_RUNNER_TOKEN": "s3cret",
            "LAZYAF_STEP_BACKEND_URL": "http://10.0.0.5:8000",
            "LAZYAF_GIT_URL_TEMPLATE": "https://git/{repo_id}.git",
            "LAZYAF_STEP_NETWORK": "lazyaf-network",
            "LAZYAF_RUNNER_ALLOW_INSECURE": "1",
            "LAZYAF_BIND_ALLOWLIST": "/var/run/docker.sock, /data",
            "LAZYAF_EXPECT_IMAGES": "lazyaf-base:dev,lazyaf-test-runner:dev",
        }
    )
    assert config.backend_url == "https://lazyaf.example.com"
    assert config.runner_id == "pi-workshop-1"
    assert config.name == "Workshop Pi"
    assert config.labels["has"] == ["gpio", "camera"]
    assert config.labels["zone"] == "workshop"
    assert config.token == "s3cret"
    assert config.step_backend_url == "http://10.0.0.5:8000"
    assert config.git_url_template == "https://git/{repo_id}.git"
    assert config.step_network == "lazyaf-network"
    assert config.allow_insecure is True
    assert config.bind_allowlist == ("/var/run/docker.sock", "/data")
    assert config.expect_images == ("lazyaf-base:dev", "lazyaf-test-runner:dev")


def test_from_env_defaults() -> None:
    config = RunnerConfig.from_env({})
    assert config.backend_url == "http://localhost:8000"
    assert config.runner_type == "generic"
    assert config.orchestrator == "docker"
    assert config.token == DEFAULT_RUNNER_TOKEN
    assert config.step_network == "bridge"
    assert config.allow_insecure is False
    assert config.bind_allowlist == ()


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True),
                                          ("0", False), ("", False), ("no", False)])
def test_boolean_env_parsing(raw, expected) -> None:
    assert RunnerConfig.from_env({"LAZYAF_RUNNER_ALLOW_INSECURE": raw}).allow_insecure is expected


def test_redacted_never_shows_the_token() -> None:
    blob = str(RunnerConfig(token="hunter2").redacted())
    assert "hunter2" not in blob
    assert "<set>" in blob
    assert "<default>" in str(RunnerConfig().redacted())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_overrides_env() -> None:
    config = config_from_args(
        ["--backend-url", "https://cli.example.com", "--runner-id", "cli-runner"],
        env={"LAZYAF_BACKEND_URL": "http://env:8000", "LAZYAF_RUNNER_ID": "env-runner"},
    )
    assert config.backend_url == "https://cli.example.com"
    assert config.runner_id == "cli-runner"
    assert config.name == "cli-runner"


def test_cli_labels_replace_env_labels_but_keep_arch() -> None:
    config = config_from_args(
        ["--labels", "zone=lab"], env={"LAZYAF_RUNNER_LABELS": "zone=workshop,has=gpio"}
    )
    assert config.labels["zone"] == "lab"
    assert config.labels["has"] == "gpio"
    assert config.labels["arch"] == platform.machine()


def test_cli_orchestrator_feeds_the_default_runner_id() -> None:
    config = config_from_args(["--orchestrator", "docker"], env={})
    assert config.runner_id == f"{socket.gethostname()}-docker"


def test_cli_exposes_every_documented_flag() -> None:
    options = {
        action.option_strings[0]
        for action in build_parser()._actions
        if action.option_strings
    }
    assert {
        "--backend-url",
        "--runner-id",
        "--name",
        "--type",
        "--labels",
        "--orchestrator",
        "--token",
        "--step-backend-url",
        "--log-level",
        "--version",
    } <= options

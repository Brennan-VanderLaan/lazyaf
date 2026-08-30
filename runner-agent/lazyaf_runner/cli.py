"""``lazyaf-runner`` entry point - Phase 12.6, section 4.5.

Env first (``RunnerConfig.from_env``), CLI flags override. Every flag maps to
exactly one env var and nothing is computed twice.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .client import EXIT_FATAL, RunnerClient
from .config import ConfigError, RunnerConfig, parse_labels
from .orchestrator.base import OrchestratorUnavailable
from .orchestrator.registry import ORCHESTRATORS, build_orchestrator

logger = logging.getLogger("lazyaf_runner")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyaf-runner",
        description=(
            "LazyAF remote runner agent: connects to a LazyAF backend over "
            "WebSocket and executes pipeline steps on this host."
        ),
    )
    parser.add_argument("--version", action="version", version=f"lazyaf-runner {__version__}")
    parser.add_argument("--backend-url", help="LAZYAF_BACKEND_URL")
    parser.add_argument("--runner-id", help="LAZYAF_RUNNER_ID (stable across restarts)")
    parser.add_argument("--name", help="LAZYAF_RUNNER_NAME")
    parser.add_argument("--type", dest="runner_type", help="LAZYAF_RUNNER_TYPE")
    parser.add_argument(
        "--labels",
        help="LAZYAF_RUNNER_LABELS, e.g. 'has=gpio,has=camera,zone=workshop'",
    )
    parser.add_argument(
        "--orchestrator",
        choices=sorted(ORCHESTRATORS),
        help="LAZYAF_ORCHESTRATOR",
    )
    parser.add_argument("--token", help="LAZYAF_RUNNER_TOKEN (shared enrollment secret)")
    parser.add_argument(
        "--step-backend-url",
        help=(
            "LAZYAF_STEP_BACKEND_URL: backend URL the STEP CONTAINER should use. "
            "Set this whenever the backend's own container_backend_url is not "
            "routable from this host."
        ),
    )
    parser.add_argument("--log-level", help="LAZYAF_RUNNER_LOG_LEVEL")
    return parser


def config_from_args(argv: list[str] | None = None, env: dict | None = None) -> RunnerConfig:
    args = build_parser().parse_args(argv)
    config = RunnerConfig.from_env(env)
    # Applied before __post_init__-derived defaults matter: orchestrator feeds
    # the default runner_id, so it has to be settled first.
    if args.orchestrator:
        config.orchestrator = args.orchestrator
    for attr, value in (
        ("backend_url", args.backend_url),
        ("runner_type", args.runner_type),
        ("token", args.token),
        ("step_backend_url", args.step_backend_url),
        ("log_level", args.log_level),
    ):
        if value:
            setattr(config, attr, value)
    if args.labels:
        merged = dict(config.labels)
        merged.pop("arch", None)
        merged.update(parse_labels(args.labels))
        config.labels = merged
    if args.runner_id:
        config.runner_id = args.runner_id
        if not args.name:
            config.name = args.runner_id
    if args.name:
        config.name = args.name
    # Re-derive the defaults that depend on the overrides above (idempotent).
    config.apply_defaults()
    return config


async def run_agent(config: RunnerConfig) -> int:
    try:
        orchestrator = build_orchestrator(config)
        await orchestrator.preflight()
    except OrchestratorUnavailable as exc:
        # Refusing to register beats registering and failing every assignment:
        # a runner that appears in the list and then eats work is harder to
        # diagnose than one that never appears and says why.
        logger.error("Cannot start runner: %s", exc)
        return EXIT_FATAL

    client = RunnerClient(config, orchestrator)

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, client.stop)
        except NotImplementedError:
            # Windows: signal handlers are not settable on the proactor loop.
            # KeyboardInterrupt still unwinds `main`, which is enough.
            pass

    try:
        return await client.run()
    finally:
        await orchestrator.shutdown()


def main(argv: list[str] | None = None) -> int:
    try:
        config = config_from_args(argv)
        # Validated HERE, before build_orchestrator/preflight touches a docker
        # daemon: a bad backend URL should say so, not surface as an unrelated
        # docker error on a host whose docker is fine. RunnerClient.run()
        # validates again - it is also entered from tests and embedders.
        config.validate()
    except ConfigError as exc:
        print(f"lazyaf-runner: {exc}", file=sys.stderr)
        return EXIT_FATAL
    logging.basicConfig(
        level=getattr(logging, str(config.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run_agent(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

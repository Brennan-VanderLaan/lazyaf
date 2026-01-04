"""
CLI for LazyAF Runner Agent.

Usage:
    lazyaf-runner --backend-url http://localhost:8000 --name "My Runner"

Or with environment variables:
    export LAZYAF_BACKEND_URL=http://localhost:8000
    export LAZYAF_RUNNER_NAME="My Runner"
    lazyaf-runner
"""

import argparse
import asyncio
import logging
import sys

from .agent import RunnerAgent
from .config import RunnerConfig


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="LazyAF Runner Agent - connects to LazyAF backend and executes steps"
    )

    parser.add_argument(
        "--backend-url",
        help="URL of LazyAF backend (default: LAZYAF_BACKEND_URL env or http://localhost:8000)",
    )
    parser.add_argument(
        "--runner-id",
        help="Unique runner ID (default: auto-generated)",
    )
    parser.add_argument(
        "--name",
        help="Human-readable runner name (default: LAZYAF_RUNNER_NAME env)",
    )
    parser.add_argument(
        "--type",
        dest="runner_type",
        help="Runner type: claude-code, gemini, generic (default: claude-code)",
    )
    parser.add_argument(
        "--labels",
        help="Labels in format: key=value,key2=value2 (use : for list values: has=gpio:camera)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def parse_labels(labels_str: str) -> dict:
    """Parse labels string into dict."""
    if not labels_str:
        return {}

    labels = {}
    for item in labels_str.split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            if ":" in value:
                labels[key.strip()] = value.strip().split(":")
            else:
                labels[key.strip()] = value.strip()
    return labels


def main():
    """Main entry point."""
    args = parse_args()
    setup_logging(args.verbose)

    # Build config from args
    config_kwargs = {}

    if args.backend_url:
        config_kwargs["backend_url"] = args.backend_url
    if args.runner_id:
        config_kwargs["runner_id"] = args.runner_id
    if args.name:
        config_kwargs["runner_name"] = args.name
    if args.runner_type:
        config_kwargs["runner_type"] = args.runner_type
    if args.labels:
        config_kwargs["labels"] = parse_labels(args.labels)

    config = RunnerConfig(**config_kwargs)

    # Create and run agent
    agent = RunnerAgent(config)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")


if __name__ == "__main__":
    main()

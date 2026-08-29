#!/bin/bash
# LazyAF base image entrypoint (runs as root, drops to lazyaf via gosu).
#
# Responsibilities (the chown-at-entrypoint fix):
# 1. Fresh named volumes are root-owned; make the workspace writable for the
#    non-root lazyaf user. TOP-LEVEL only — never recurse into repo/ (it can
#    be huge) and idempotent across steps.
# 2. Ensure the HOME subdirectory skeleton exists (pip/npm/uv cache targets).
# 3. Mode switch: LAZYAF_CONTROL=1 -> control runtime (/control/run.py);
#    anything else -> CMD passthrough, so lazyaf-base degrades to a stock
#    image (what the per-step `control: false` escape hatch rides on).
set -e

mkdir -p \
    /workspace/repo \
    /workspace/.control \
    /workspace/home/.cache/pip \
    /workspace/home/.config \
    /workspace/home/.local/bin \
    /workspace/home/.local/share \
    /workspace/home/.npm-global/bin

chown lazyaf:lazyaf \
    /workspace \
    /workspace/repo \
    /workspace/.control \
    /workspace/home \
    /workspace/home/.cache \
    /workspace/home/.cache/pip \
    /workspace/home/.config \
    /workspace/home/.local \
    /workspace/home/.local/bin \
    /workspace/home/.local/share \
    /workspace/home/.npm-global \
    /workspace/home/.npm-global/bin

# Configs delivered by put_archive may carry foreign ownership; fix them so
# the runtime (uid 1000) can read and consume-once-delete them. Covers the
# per-step <step_execution_id>.json path (12.3 cross-agent contract #1) and
# the legacy step_config.json alike.
find /workspace/.control -maxdepth 1 -name '*.json' -exec chown lazyaf:lazyaf {} + 2>/dev/null || true

# gosu resets HOME to the passwd entry (/home/lazyaf), clobbering the baked
# ENV HOME=/workspace/home and any explicit HOME the executor passed - which
# silently breaks the 12.3 cross-step HOME-persistence contract in BOTH
# modes (found by tdd/integration/services/test_home_persistence.py).
# Re-assert the CONTAINER's HOME (baked default or step override) across the
# privilege drop via env(1).
STEP_HOME="${HOME:-/workspace/home}"

if [ "${LAZYAF_CONTROL:-0}" = "1" ]; then
    exec gosu lazyaf env HOME="$STEP_HOME" python3 /control/run.py
fi

if [ "$#" -eq 0 ]; then
    exec gosu lazyaf env HOME="$STEP_HOME" bash
fi

exec gosu lazyaf env HOME="$STEP_HOME" "$@"

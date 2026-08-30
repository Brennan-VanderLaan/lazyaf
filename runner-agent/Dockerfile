# LazyAF runner agent - Phase 12.6
#
# Runs on a machine the backend does not own. It needs only Python and access
# to whatever its orchestrator drives; for the docker orchestrator that means a
# reachable docker socket (the same deliberate DooD tradeoff the T2 tier step
# already carries).
#
# It deliberately does NOT bake git or the lazyaf-* step images: the workspace
# clone runs in its own helper container, and image provenance stays with
# build_images.py (section 7.1, tree-hash determinism). A host missing an
# expected image advertises has=[images:stale] rather than pulling behind the
# operator's back.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LAZYAF_ORCHESTRATOR=docker

WORKDIR /app

COPY pyproject.toml README.md ./
COPY lazyaf_runner ./lazyaf_runner

RUN pip install --no-cache-dir .

# Runs as root by default: the docker socket's group differs per host, and the
# 12.3 lesson is that the fix belongs at the entrypoint that can read the
# socket's ACTUAL gid, never a hardcoded one. Compose deployments that care
# should set `user:` and `group_add:` from their own host's socket gid.
ENTRYPOINT ["lazyaf-runner"]

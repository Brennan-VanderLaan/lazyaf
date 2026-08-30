# LazyAF Agent Base Image (Phase 12.5)
# Build: python scripts/build_images.py  (tag: lazyaf-agent-base:dev)
#
# The agent RUNTIME layer: lazyaf-base plus runner-common installed
# system-wide, so `python3 -m runner_common.agent_wrapper` — the fixed
# command the backend puts in every agent step's config — resolves.
#
# It is ALSO the mock agent image (`agent: mock` -> lazyaf-agent-base:dev):
# the mock executor needs python + runner-common and nothing else, so a
# fourth image whose only content is "agent-base minus nothing" would be a
# rebuild cost with no payload.
#
# Inherits from base: control layer + entrypoint, lazyaf.control-layer=1
# (so image_supports_control_layer needs no change), the HOME persistence
# env block, and the gosu drop to uid 1000. No USER directives here.

FROM lazyaf-base:dev

# runner-common is STAGED into this build context by scripts/build_images.py
# (stage_context copies REPO_ROOT/runner-common -> <ctx>/runner-common minus
# STAGE_EXCLUDE). There is NO copy of it under images/ in git: one source
# for the executors (R3), and editing runner-common/tests never churns this
# image's content hash.
COPY runner-common/ /opt/runner-common/

# PIP_USER=0 overrides the baked PIP_USER=1 (which targets the runtime
# volume, shadowed at run time): build-time installs must land in the image,
# not in /workspace.
RUN PIP_USER=0 pip install --no-cache-dir /opt/runner-common

# Verify the wrapper entrypoint resolves (build fails loudly if packaging
# regressed — the failure mode this replaces is a runtime
# `ModuleNotFoundError: runner_common` 30 seconds into a step).
RUN PYTHONDONTWRITEBYTECODE=1 python3 -c "import runner_common.agent_wrapper as w; assert callable(w.main)"

# Positive capability declaration, used by ONE preflight assertion (an agent
# step pointed at lazyaf-test-runner:dev gets a clear message) — never by
# mode selection, which stays on the inherited lazyaf.control-layer label.
ARG CONTENT_HASH=dev
LABEL lazyaf.agent-runtime=1
LABEL lazyaf.content-hash=$CONTENT_HASH

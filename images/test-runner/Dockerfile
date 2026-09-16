# LazyAF Test Runner Image (Phase 12.3; Go toolchain added for the TG tier)
# Build: python scripts/build_images.py  (tag: lazyaf-test-runner:dev)
#
# Extends lazyaf-base with:
# - uv (fast Python package manager)
# - Common test tools (pytest, coverage, etc.)
# - Node.js + vitest for frontend testing
# - Go (exactly the go.mod `toolchain` version, GOTOOLCHAIN=local) and
#   shellcheck, for the Go CLI tier (scripts/run_tier.py TG,
#   upcoming/go-cli.md §10.1)
#
# Use this for CI/CD pipelines that run Python, JavaScript and Go tests (the
# dogfood pipeline's sync-deps/tier steps run on this image).
# No USER directives — the base entrypoint drops to lazyaf via gosu.

FROM lazyaf-base:dev

# Install Node.js 20.x for frontend testing
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv into the image (NOT $HOME — the baked HOME is the runtime
# volume path, shadowed at run time)
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# Go toolchain for the TG tier (upcoming/go-cli.md §2.2, §10.1).
#
# GO_VERSION is PINNED to go.mod's `toolchain` line by
# tdd/unit/control_runtime/test_image_contract.py, and GOTOOLCHAIN=local
# (below) is what makes that pin bite: a future go.mod bump beyond this image
# fails with `go: go.mod requires go >= X (running Y; GOTOOLCHAIN=local)`
# instead of downloading a toolchain mid-tier (R1). The tarball's sha256 is
# verified before extraction; the two sums are go.dev/dl's published values
# for this release.
#
# Not staged here on purpose: no go.mod, no pre-warmed module cache. HOME is
# /workspace/home (base image), so GOMODCACHE and GOCACHE land on the run's
# persistent workspace volume and the handful of modules download once per
# worker - a staged go.mod would restamp this image on every dependency bump.
ARG GO_VERSION=1.26.8
ARG GO_SHA256_AMD64=d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b
ARG GO_SHA256_ARM64=211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) sum="$GO_SHA256_AMD64";; arm64) sum="$GO_SHA256_ARM64";; \
      *) echo "no Go build for $arch" >&2; exit 1;; esac; \
    curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz"; \
    echo "$sum  /tmp/go.tgz" | sha256sum -c -; \
    tar -C /usr/local -xzf /tmp/go.tgz; rm /tmp/go.tgz
# shellcheck: cli/install.sh is linted in the TG tier (§6); the Debian package
# is the pinned-by-distro version, no third-party download.
RUN apt-get update && apt-get install -y --no-install-recommends shellcheck && rm -rf /var/lib/apt/lists/*
# GOFLAGS=-mod=readonly: a test can never rewrite go.sum (Go's default since
# 1.16, made explicit). CGO_ENABLED=0: the binary is static and the tier
# never runs -race (which needs CGO) - stated in §1.4.
ENV PATH="/usr/local/go/bin:$PATH" GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0

# Pre-install common Python test tools SYSTEM-WIDE.
# - PIP_USER=0 overrides the baked PIP_USER=1 (which targets the runtime
#   volume) for this build-time install.
# - "pytest>=7.0" is QUOTED: unquoted, `>=7.0` is a shell redirect that
#   installs bare `pytest` and writes a file named `=7.0`.
RUN PIP_USER=0 pip install --no-cache-dir \
    "pytest>=7.0" \
    pytest-cov \
    pytest-asyncio \
    coverage

# Pre-install common npm tools into the image (see NPM_CONFIG_PREFIX note)
RUN NPM_CONFIG_PREFIX=/usr/local npm install -g vitest

# Verify installations (build fails loudly if a tool is missing)
RUN python --version \
    && node --version \
    && npm --version \
    && uv --version \
    && pytest --version \
    && go version \
    && shellcheck --version

# Staleness label (stamped by scripts/build_images.py; includes base's hash)
ARG CONTENT_HASH=dev
LABEL lazyaf.content-hash=$CONTENT_HASH

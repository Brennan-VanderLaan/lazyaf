# LazyAF Debug Sidecar Image (Phase 12.7)
# Build: python scripts/build_images.py   (tag: lazyaf-debug-sidecar:dev)
#
# The container a developer attaches to at a breakpoint. It mounts the paused
# run's WORKSPACE VOLUME at /workspace and runs an interactive shell there, so
# the user sees exactly the bytes the resumed step is about to see.
#
# WHY FROM lazyaf-base:dev AND NOT ubuntu:22.04 (failure_01's choice):
# the workspace volume is owned by uid/gid 1000 (`lazyaf`), pinned by the base
# image precisely so put_archive and chown are deterministic. A root Ubuntu
# sidecar can read everything, but every file it CREATES is root-owned, and
# the resumed step - which runs as uid 1000 - then trips over them. Deriving
# from base inherits the user, the /workspace layout, the HOME/XDG env block
# and the entrypoint's chown+gosu drop, and it joins the multi-level
# content-hash chain (a base change restamps this image too).
#
# Contract:
# - LABEL lazyaf.control-layer=0. DELIBERATE and the opposite of every other
#   image in the tree: this container runs no steps, so the executor must
#   never select control mode for it. `0` is a stated fact, not an omission -
#   an image with no label at all would be indistinguishable from one whose
#   author forgot.
# - LABEL lazyaf.debug-sidecar=1 is the positive marker the sweeper and the
#   session service select on.
# - LAZYAF_CONTROL is never set for a sidecar, so the base entrypoint takes
#   its CMD-passthrough branch: `exec gosu lazyaf env HOME=... sleep infinity`.
#   The container therefore idles as uid 1000 with the workspace already
#   chowned, and the terminal service execs into it as 1000:1000.
# - The /workspace mount is READ-WRITE. That is the point of a debug re-run
#   and also its sharp edge, so the MOTD says so in as many words.

FROM lazyaf-base:dev

# Debug toolbelt. base already provides git, curl, sudo, gosu and python3, so
# only the interactive-inspection layer is added here.
#   procps  - ps/top, absent from python:*-slim
#   file    - "what IS this artifact the step produced"
#   tmux    - survive a dropped CLI attach mid-command
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    vim \
    nano \
    less \
    htop \
    tree \
    jq \
    file \
    procps \
    bash-completion \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# The banner goes into /etc/bash.bashrc, NOT /home/lazyaf/.bashrc: the base
# image bakes HOME=/workspace/home, which is a MOUNTED VOLUME at run time, so
# anything written to a home directory during the build is shadowed and never
# read. /etc/bash.bashrc is sourced by every interactive non-login bash
# regardless of HOME. The guard makes it print once per attach rather than
# once per nested shell.
COPY motd /etc/lazyaf-debug-motd
RUN printf '%s\n' \
    '' \
    '# LazyAF debug sidecar banner (Phase 12.7)' \
    'if [ -z "${LAZYAF_DEBUG_MOTD_SHOWN:-}" ] && [ -r /etc/lazyaf-debug-motd ]; then' \
    '    export LAZYAF_DEBUG_MOTD_SHOWN=1' \
    '    cat /etc/lazyaf-debug-motd' \
    'fi' \
    'export PS1="\[\033[01;33m\][debug]\[\033[00m\] \w\$ "' \
    'alias ll="ls -la"' \
    'alias la="ls -A"' \
    >> /etc/bash.bashrc

# Capability + staleness labels. control-layer=0 OVERRIDES the base's =1.
ARG CONTENT_HASH=dev
LABEL lazyaf.control-layer=0
LABEL lazyaf.debug-sidecar=1
LABEL lazyaf.content-hash=$CONTENT_HASH

# Attach lands the user in the repo, which is what they came to look at.
WORKDIR /workspace/repo

# Idle forever; the terminal service execs a login shell into this container.
# The base ENTRYPOINT (chown + gosu drop to uid 1000) is inherited and kept -
# no USER directive, because the drop happens at run time after the chown.
CMD ["sleep", "infinity"]

# LazyAF release CI

**These workflows package LazyAF. They do not gate it.**

LazyAF gates LazyAF. Whether the code is correct is decided by LazyAF's own
dogfood pipeline running against this repository — PLAN.md's standing decision
("no external CI ever; self-host ASAP") is about **test gating**, and it has
not changed. Nothing in `.github/` votes on whether a change is good.

What lives here is **release engineering**: taking a revision the dogfood
pipeline already blessed and turning it into artifacts a stranger can install
and run — a wheel, a set of container images, and a compose file that pulls
them. Two rules follow from that, and both are repeated at the top of every
workflow file so nobody has to find this document to learn them:

1. **Do not add the test suite to any of these workflows.** The moment `pytest`
   runs here, GitHub becomes the quality gate and the standing decision has
   been reversed by accident.
2. **The only things allowed to block a publish** are "the artifact could not
   be produced" and "the artifact contains a credential".

---

## The files

| File | Trigger | What it does |
|---|---|---|
| `workflows/pr-build.yml` | PR to `main`, manual | Builds every release artifact and scans the images. Pushes nothing. Not run on `main` itself &mdash; `images.yml` already builds everything there. |
| `workflows/images.yml` | push to `main`, tag `v*`, manual | Builds and **pushes** every image to GHCR. |
| `workflows/release.yml` | tag `v*`, manual | Builds the CLI wheel, attaches it (plus the onboarding files) to a GitHub Release. Optional, opt-in PyPI publish. |
| `workflows/secret-scan.yml` | called by the three above; also manual | The leak gate. Reusable, so there is one definition and no drift. |
| `dependabot.yml` | monthly | Keeps the pinned action SHAs current. `github-actions` only. |

| Script | Purpose |
|---|---|
| `scripts/secret_patterns.py` | One definition of "what a leaked key looks like", plus the exact-value allowlist. Imported by both scanners. |
| `scripts/scan_repo_secrets.py` | Fails if the source tree contains a live-format credential, an untracked-but-committed `.env`, or a loosened `.gitignore`. |
| `scripts/scan_image_secrets.py` | Fails if a built image bakes a credential or contains a `.env`. |
| `scripts/step_images.py` | Reads the step-image list out of `scripts/build_images.py`'s `IMAGES` table. |
| `scripts/publish_image.py` | Owns the GHCR tag policy; tags and pushes one image. |
| `scripts/check_release_version.py` | Fails a release whose wheel version disagrees with the git tag. |

---

## Security posture

This is the hard requirement, so it is stated concretely rather than as a
principle.

**No secret is needed to build anything.** Not the wheel, not any image. The
AI provider keys are a *run-time* input supplied by the operator's `.env`; they
are never a build-time input, never a `--build-arg`, never a BuildKit secret
mount. That is what makes "the images never bake an AI key" a structural fact
rather than a promise — there is no key in scope during a build to bake.

**The only credential that exists** is the automatic per-run `GITHUB_TOKEN`,
and it is used in exactly two places: `docker login ghcr.io` (job-scoped
`packages: write`) and `gh release` (job-scoped `contents: write`). No personal
access token. No organisation secret. Nothing long-lived anywhere.

**Least privilege.** Every workflow declares `permissions: contents: read` at
the top level. Three jobs widen it, each by one entry:

| Job | Extra permission | Why |
|---|---|---|
| `images.yml` → `step-images`, `service-images` | `packages: write` | push to GHCR |
| `release.yml` → `wheel` | `contents: write` | attach assets to the Release |
| `release.yml` → `pypi` | `id-token: write` | mint a short-lived OIDC identity for PyPI |

**`pull_request_target` is never used.** The build workflow triggers on
`pull_request`, which runs fork code with the fork's read-only token and no
access to this repository's secrets. The two publishing workflows are not
reachable from a pull request at all — they trigger only on `main`, on a tag,
or on a manual dispatch. A fork PR therefore cannot reach a job that holds a
write token, by construction rather than by an `if:` somebody could delete.

**Nothing echoes a secret.** The GHCR token is delivered to `docker login` on
stdin (`printf '%s' "$TOKEN" | docker login --password-stdin`), so it never
appears in the log, in an `argv` listing, or in shell history. Workflow inputs
reach scripts through `env:` rather than being interpolated into a command
line, so a crafted input cannot become shell syntax.

**Actions are pinned to commit SHAs**, with the version in a trailing comment.
A tag can be moved by whoever owns the action; a SHA cannot. The complete list
of third-party code that runs in these workflows:

```
actions/checkout@11d5960a326750d5838078e36cf38b85af677262          # v4.2.2
actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065      # v5.6.0
actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02   # v4.6.2
actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
```

That is deliberately short. Registry login, image pushing and release creation
are done with plain `docker` and `gh` rather than with third-party actions,
because those are the steps that hold a token.

A SHA pin never picks up a security fix on its own, so `dependabot.yml` watches
them — scoped to `github-actions` only, batched into one monthly PR. It does
**not** watch pip/npm/docker: those are LazyAF's own supply chain and belong to
the dogfood pipeline, not to a bot on GitHub. Read the action's diff before
merging a bump; a SHA change is a supply-chain change, not a chore.

---

## The two leak gates

### 1. Source scan — `scan_repo_secrets.py`

Runs in `secret-scan.yml`, which the other three workflows call. In the
publishing workflows it is a hard `needs:` — nothing is built or pushed until
it passes.

Checks:

* `.env` and `.env.local` are still ignored by git (`git check-ignore`), so
  loosening `.gitignore` fails the build rather than quietly arming the
  footgun.
* No `.env` file is tracked. `.env.example` and friends are allowed — they are
  the template a new user copies — and their contents are scanned anyway.
* No tracked file contains a live-format key (Anthropic, OpenAI, Google,
  GitHub, AWS, PEM private keys).

**The allowlist is by exact value, never by file path.** LazyAF's test suite
contains key-shaped strings on purpose: they are the sentinels that
containment tests assert never reach a log, a step container's environment, or
an API response. Those specific strings are allowlisted in
`scripts/secret_patterns.py`, each with a comment naming the test that owns it.
A real key sitting on the next line still fails. Allowlisting `tdd/` by path
would have opened a hole the size of the test suite.

Adding a new sentinel means adding its exact value to `ALLOWLIST` with a
comment. If a finding is a *real* key: rotate it first — it is already
public — and only then clean the tree.

`--history N` additionally scans the patches of the last N commits. It is off
by default because the working tree is what gets published and a history scan
needs an unshallow clone; run it on demand from the Actions tab
(`secret-scan` → Run workflow → history depth).

### 2. Image scan — `scan_image_secrets.py`

Runs against every image right after it is built, and — in `images.yml` —
before a single `docker push`. Four checks, cheapest first:

1. **Config env.** A variable whose *name* says credential (`ANTHROPIC_API_KEY`,
   `*_TOKEN`, `*_SECRET`, …) must not carry a value. This is the
   shape-independent check: it catches a leaked key from a provider nobody has
   modelled, which a regex gate would sail straight past.
2. **Labels and build history.** `docker history` records every `--build-arg`
   value interpolated into a `RUN` line — the classic "the key is in the image
   metadata forever" leak.
3. **Filenames.** A `.env` (or `.env.local`, …) *anywhere* in the filesystem
   fails. `.env.example`/`.sample`/`.template`/`.dist` are allowed.
4. **Contents.** Every text file is matched against the key formats.

Check 4 skips third-party dependency trees (`node_modules`, `site-packages`,
`/usr/lib`, apt state) by default. That is not hand-waving — with
`--include-vendor` the Claude image reports three findings, all of them npm's
own documentation printing the literal string `-----BEGIN ... PRIVATE KEY-----`
in a config example. A gate that cries wolf gets disabled, and checks 1–3
still cover those trees completely: a `.env` in `node_modules` fails, and a key
injected via env or build arg is caught wherever it landed. Use
`--include-vendor` for a deliberate deep audit.

---

## Naming and tags

Images are published to **`ghcr.io/<owner>/lazyaf/<name>`**, matching what
`docker-compose.release.yml` pulls (`${LAZYAF_IMAGE_PREFIX}/backend` etc.):

```
ghcr.io/brennan-vanderlaan/lazyaf/backend
ghcr.io/brennan-vanderlaan/lazyaf/frontend
ghcr.io/brennan-vanderlaan/lazyaf/runner-agent
ghcr.io/brennan-vanderlaan/lazyaf/base           <- step images, one per entry
ghcr.io/brennan-vanderlaan/lazyaf/agent-base        in build_images.py's
ghcr.io/brennan-vanderlaan/lazyaf/claude            IMAGES table
ghcr.io/brennan-vanderlaan/lazyaf/gemini
ghcr.io/brennan-vanderlaan/lazyaf/test-runner
ghcr.io/brennan-vanderlaan/lazyaf/debug-sidecar
```

The step-image list is **read from `scripts/build_images.py`**, never written
out in a workflow. An image added to that table is published and leak-scanned
automatically; the alternative is a hardcoded list that silently stops covering
new images while still reporting green. (The debug sidecar was added to that
table while this CI was being written, which is the drift being designed out.)

Tag policy lives in `scripts/publish_image.py` and can be inspected without a
registry:

```bash
GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.4.0 GITHUB_SHA=$(git rev-parse HEAD) \
  python .github/scripts/publish_image.py \
    --local lazyaf-base:dev --repo ghcr.io/brennan-vanderlaan/lazyaf/base --dry-run
```

| Tag | When | Meaning |
|---|---|---|
| `sha-<7>` | always | the exact commit. Immutable. Quote this in a bug report. |
| `content-<12>` | step images | the `lazyaf.content-hash` label — what is *in* the image, not which commit built it. |
| `vX.Y.Z` and `X.Y.Z` | version tag | both spellings, because half the world writes the `v`. |
| `latest` | stable version tag only | newest stable release. Prereleases (`v1.0.0-rc1`) never move it. |
| `main`, `edge` | push to `main` | tip of the default branch. Deliberately **not** `latest`. |

### About `latest`

`scripts/build_images.py` says "NO `:latest` anywhere (grep-able rule)". That
rule governs **local** step-image tags, where a moving tag would make a step
silently run yesterday's image and make the staleness check meaningless. Those
images are still `lazyaf-<name>:dev` locally and always will be. `latest` here
is a **registry** tag on published release artifacts — what a stranger types,
and what `docker-compose.release.yml` defaults to.

> **Before the first `v*` tag exists there is no `latest`.** Until then, a new
> user needs `LAZYAF_VERSION=main` in their `.env`. Cutting `v0.1.0` is what
> makes the default work.

### Step images are not compose services

The backend resolves them by their local `lazyaf-<name>:dev` tag and
deliberately never pulls on its own (it fails a step loudly with
`Image not found: lazyaf-base:dev`). So a user pulls and retags once. The
`images` workflow prints the exact commands in its run summary; the shape is:

```bash
docker pull ghcr.io/brennan-vanderlaan/lazyaf/base:latest
docker tag  ghcr.io/brennan-vanderlaan/lazyaf/base:latest lazyaf-base:dev
```

---

## Cutting a release

1. Let the dogfood pipeline go green on the revision you want to ship.
2. Bump `__version__` in `cli/lazyaf/__init__.py` (the single source of the CLI
   version; `cli/pyproject.toml` reads it via `[tool.setuptools.dynamic]`).
3. `git tag v0.2.0 && git push origin v0.2.0`.

The tag fires `release.yml` and `images.yml` in parallel. They are separate
workflows because they fail for entirely different reasons, and a broken
frontend build should not withhold the wheel.

`check_release_version.py` compares the built wheel's version against the tag
and stops the release on a mismatch. The comparison reduces both sides to
lowercase alphanumerics, so the PEP 440 spelling `1.0.0rc1` matches the tag
`v1.0.0-rc1` while `0.1.0` against `v0.2.0` fails. A published version can
never be reused, which is why this is checked before the upload rather than
noticed after it.

### PyPI (optional, opt-in, tokenless)

`release.yml` has a `pypi` job that is **off by default and unreachable from a
tag push**. It runs only via *Run workflow* with `publish_to_pypi` checked.

A GitHub Release can be deleted and re-cut; a PyPI version is permanent. Making
every tag push to a global index automatically is a one-way door that should be
opened by a person, on purpose, after the wheel has been built and smoke
tested.

It uses **PyPI Trusted Publishing (OIDC)** — no API token is stored in this
repository, so there is no PyPI credential here to leak or rotate. Before it
can work, once, by hand:

1. On PyPI: project `lazyaf-cli` → Publishing → add a trusted publisher with
   owner `Brennan-VanderLaan`, repository `lazyaf`, workflow `release.yml`,
   environment `pypi`.
2. On GitHub: Settings → Environments → create `pypi`, and add yourself as a
   required reviewer so publishing also needs an explicit approval click.

Until step 1 exists the job fails at the upload, which is correct: nothing
half-published.

---

## Testing these workflows

GitHub Actions cannot be run locally, so everything that *can* be checked
without GitHub is checked without GitHub.

**Validate the YAML and the shell.** Every file parses, no
`pull_request_target`, every action SHA-pinned, every `run:` block valid bash:

```bash
python - <<'EOF'
import re, subprocess, sys, yaml
from pathlib import Path
EXPR = re.compile(r"\$\{\{[^}]*\}\}")
SHA = re.compile(r"^[^@]+@[0-9a-f]{40}(\s+#.*)?$")
bad = []
for f in sorted(Path(".github/workflows").glob("*.yml")):
    doc = yaml.safe_load(f.read_text())          # parses at all?
    trig = doc.get("on", doc.get(True))          # PyYAML reads `on:` as True
    if "pull_request_target" in trig: bad.append(f"{f.name}: pull_request_target")
    if "permissions" not in doc: bad.append(f"{f.name}: no top-level permissions")
    def walk(n, key, out):
        if isinstance(n, dict):
            for k, v in n.items():
                out.append(v) if k == key and isinstance(v, str) else walk(v, key, out)
        elif isinstance(n, list):
            for v in n: walk(v, key, out)
    uses, runs = [], []
    walk(doc, "uses", uses); walk(doc, "run", runs)
    bad += [f"{f.name}: unpinned {u}" for u in uses
            if not u.startswith("./") and not SHA.match(u)]
    for script in runs:
        p = subprocess.run(["bash", "-n"], input=EXPR.sub("EXPR", script).encode(),
                           capture_output=True)
        if p.returncode: bad.append(f"{f.name}: bash syntax\n{p.stderr.decode()}")
    print(f"[ok] {f.name}: {len(uses)} uses, {len(runs)} run blocks")
print("\n".join(bad) or "ALL CHECKS PASSED"); sys.exit(1 if bad else 0)
EOF
```

**Exercise each script directly** — this is most of what the workflows do:

```bash
# the source leak gate (fast; add --history 200 for an audit)
python .github/scripts/scan_repo_secrets.py

# the image leak gate, against images you already have locally
python .github/scripts/scan_image_secrets.py $(python .github/scripts/step_images.py --refs)

# the tag policy, without a registry
GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.4.0 GITHUB_SHA=$(git rev-parse HEAD) \
  python .github/scripts/publish_image.py --local lazyaf-base:dev \
    --repo ghcr.io/brennan-vanderlaan/lazyaf/base --dry-run

# the release version check
python -m build --outdir /tmp/dist cli/
python .github/scripts/check_release_version.py --tag v0.1.0 --dist /tmp/dist
```

**Prove the image scanner still bites.** A gate nobody has seen fail is a gate
nobody should trust:

```bash
mkdir /tmp/poison && cd /tmp/poison
# The fake key is GENERATED, never written down: a literal key-shaped string
# in this file would (correctly) fail the source scan described above.
FAKE="sk-ant-api03-$(printf 'A%.0s' $(seq 95))"
printf 'ANTHROPIC_API_KEY=%s\n' "$FAKE" > payload
cat > Dockerfile <<'EOF'
FROM alpine:3.20
ARG FAKE_KEY=unset
ENV ANTHROPIC_API_KEY=$FAKE_KEY
COPY payload /app/.env
EOF
docker build --build-arg "FAKE_KEY=$FAKE" -t lazyaf-scanner-selftest:poison .
python /path/to/lazyaf/.github/scripts/scan_image_secrets.py lazyaf-scanner-selftest:poison
# expect: exit 1, findings tagged [env], [history], [dotenv] and [content]
docker rmi -f lazyaf-scanner-selftest:poison
```

**First real run.** Use *Run workflow* on `images` with **dry run** checked: it
builds and scans everything and prints the tags it *would* push, without
touching the registry or even logging in.

---

## Known limitations and follow-ups

* **`linux/amd64` only.** Cross-building the agent images for arm64 means
  emulating a Node toolchain under QEMU (a 20-minute job becomes an hour-plus),
  and `build_images.py` drives the classic build API rather than buildx, so
  multi-arch is a change *there* rather than here. An arm64 user builds
  locally today.
* **`build_images.py` uses the docker SDK's classic build endpoint.** It works
  on today's runners. If a future Docker Engine drops that endpoint, the fix is
  a buildx path in `build_images.py`, not a workflow change.
* **`backend/Dockerfile` pulls `ghcr.io/astral-sh/uv:latest`.** A floating tag
  in a release image build is a supply-chain loose end; pinning it to a digest
  is a one-line change in that file, which belongs to the backend lane.
* **PR builds are not cheap** — every image is built from scratch on every PR.
  If that becomes annoying, the honest fix is a registry-backed build cache,
  not dropping the coverage: the whole point is catching a broken Dockerfile
  before a tag.
* **Turn on GitHub's own push protection** as well. `scan_repo_secrets.py` is a
  gate on our terms; secret scanning is a second, independent one.

---

## The `.dockerignore` files

Three were added alongside this CI (`frontend/`, `backend/`, `runner-agent/`),
because a build context is the easiest way for a credential to enter a public
image and none of the three had a filter.

`frontend/` also fixes a real bug: the Dockerfile does `COPY . .` *after*
`npm ci`, so a host `node_modules/` was being copied over the one the image had
just installed — a Windows dependency tree, native binaries and all, landing on
top of the Linux one. Excluding `node_modules` is what makes that `npm ci`
actually count.

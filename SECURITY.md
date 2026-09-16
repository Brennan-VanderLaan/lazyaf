# Security

## What LazyAF is

A self-hosted CI/CD platform whose pipeline steps can be AI agents. It hosts its
own git server, and a push to it triggers it. Steps run in Docker containers
that the backend creates through a mounted docker socket.

Executing code you did not write is the product, not a side effect.

## The deployment shape it assumes

One operator, one trusted network, one host. A single-worker uvicorn process, a
SQLite database, and a docker socket. **Everyone who can reach the port is
assumed to be the operator.** There is no user model, no roles, and no tenancy,
and nothing in the codebase pretends otherwise.

## What LazyAF defends against

- **Credential leakage into places credentials get pasted.** The bug-report
  bundle redacts before you attach it. Model-endpoint API keys are never stored
  in the database — only a reference to an allowlisted environment variable.
  Endpoint error fields are scrubbed on write.
- **Provider keys appearing in `docker inspect` of a step container.** They are
  delivered through a 0600 file that the step runtime consumes and deletes; if a
  secret would have to travel in inspectable container environment, dispatch
  refuses instead of downgrading.
- **Plaintext runner enrollment.** A runner refuses `ws://` to a non-loopback
  backend without an explicit override, and refuses the retired public
  development secret by value.
- **Half-configured secrets.** The shared secrets have no defaults. The process
  does not start without them, and known-public historical values are rejected
  exactly as an empty value is.
- **Publishing a leaked credential.** CI refuses to publish an image or a
  release if the tree or the built image layers contain a live-format
  credential.
- **A tampered CLI download.** From v0.3.0 the release ships the `lazyaf`
  binaries with a `checksums.txt` written by the same job that built them;
  `install.sh` computes the sha256 of what it downloaded and stops before
  installing anything if it does not match, and there is no `--insecure` to
  skip that. The installed binary is then run and its reported version
  compared to the requested one. There is no self-update path to secure:
  re-running the install line is the upgrade. (The Python CLI this replaces
  is still in the tree until the cutover and has no such check; if a
  `pip install ./cli` copy sits earlier on PATH than the verified binary,
  `install.sh` says so and names it rather than letting it win silently.)

## What LazyAF explicitly does NOT defend against

**The API has no authentication.** Every human-facing route is open to anyone
who can reach the port: read every repo, card, pipeline and model endpoint;
create a diagnostics bundle summarising the whole system; mint a debug terminal
token; push to the git server; trigger a pipeline.

> Do not expose this to the internet. Do not expose it to a network you do not
> control. If you need it reachable, put it behind a reverse proxy that
> authenticates, and understand that you are the one providing the
> authentication.

**A pipeline file in a repo is arbitrary code on your machine.** Anyone who can
push can run a step. Only host repositories you would grant a shell to.

A step can also ask for host paths via `needs: [docker]`, and the docker socket
is root on the host. That is **opt-in** — `LAZYAF_STEP_BIND_ALLOWLIST` is empty
by default and a step that asks without it fails loudly. If you set it, you are
granting host root to every pipeline definition that can reach this server,
including ones pushed by someone else.

**Agent steps are not sandboxed from their own input.** Agents run with
permission prompts disabled — that is what makes them autonomous — so an
instruction hidden in a card description or a repo file is executed with the
same authority as one you wrote. That authority includes the step's own API key,
the repository, the backend, and the internet.

**There is no secret-at-rest story.** The database is a plain SQLite file,
backups are plain files, and step logs are stored verbatim. If a step prints a
credential, that credential is in the database. Treat the data directory as
being as sensitive as the secrets you configured.

## If you ignore one thing here, do not let it be this one

**Leave the ports bound to localhost.**

They ship that way — `LAZYAF_BACKEND_PORT` and `LAZYAF_FRONTEND_PORT` default to
`127.0.0.1:8000` and `127.0.0.1:5173`, and both take the full `[HOST_IP:]PORT`
form. Removing the `127.0.0.1:` publishes an API with no authentication to that
network. Everything else on this page is a consequence of who can reach the
port, so this is the control that decides how much the rest matters.

## Reporting a vulnerability

Open an issue at https://github.com/Brennan-VanderLaan/lazyaf/issues. This is a
single-maintainer hobby project with no security SLA and no embargo process;
please say so plainly in the title if you believe something is exploitable, and
do not include real credentials in the report.

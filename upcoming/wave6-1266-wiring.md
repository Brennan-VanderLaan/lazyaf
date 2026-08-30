# Wave 6 - Phase 12.6.6 Wiring Design: Spec-curated agent context

Status: DESIGN - the implementer builds from this verbatim.
Lane: B. Migration claim: **NONE - 0008 is released back to the pool** (section 8).

Inputs read:
`PLAN.md` line 961 (roadmap entry) and line 3215 (`### Phase 12.6.6: Spec-Curated Agent Context`),
`backend/app/services/control_layer/workspace.py` (`generate_agent_config`, the single producer),
`runner-common/runner_common/agent_config.py` + `agent_wrapper.py` (the single consumer),
`backend/app/services/agent_prompt.py` (the single prompt renderer),
`backend/app/services/pipeline_executor.py` (`_attach_agent_payload`),
`backend/app/services/execution/local_executor.py` (`build_control_archive`, `CONTROL_CONFIG_DIR`),
`backend/app/services/execution/runner_protocol.py` (`build_execute_step_config`),
`backend/app/services/agent_run.py` (`start_card_work` -> `start_adhoc_agent_run` -> `build_agent_step_config`),
`backend/app/models/spec.py`, `backend/app/models/testref.py`, `backend/app/models/card.py`,
`backend/app/services/test_ingestion.py`, `runner-common/runner_common/pytest_lazyaf.py`,
`runner-common/runner_common/executors/{base,claude}.py`,
`images/base/control/entrypoint.sh`, `tdd/unit/control_runtime/test_agent_config_contract.py`,
`runner-common/tests/test_agent_wrapper.py`, `scripts/run_tier.py`,
`upcoming/wave5-126-wiring.md` (house style).

---

## 0. Ground truth found during recon (read this before arguing with the design)

1. **The agent prompt is one argv element.** `runner_common/executors/claude.py:74`
   builds `["claude", "-p", config.prompt, ...]`. Linux `MAX_ARG_STRLEN` is a hard
   **131072 bytes for a single argv element** (32 pages, not tunable). The prompt
   already carries up to `PREVIOUS_STEP_LOGS_MAX_BYTES` = 32 KiB of previous-step
   logs plus an unbounded card description. The token budget in this phase is not a
   nicety - **it is the thing that keeps the agent step from dying with E2BIG.**
   That fixes the order of magnitude of the cap (section 3.1).

2. **The codebase already decided the wrapper does NOT re-template.**
   `agent_prompt.py` docstring, verbatim: *"The backend already owns `PromptTemplate`,
   the card fields, the resolved AgentFile definitions and (at 12.6.6) the spec
   bundle. A container that re-templates is a second source of truth for that
   string."* And `agent_config.py:210`: *"the agent config - which carries the
   rendered prompt and, at 12.6.6, curated spec context - must not survive it."*
   Both seams were cut for this phase in 12.5. PLAN's line *"Claude/Gemini wrappers
   prepend spec_context.md to system prompt"* predates that and is superseded
   (deviation D2, section 9).

3. **The control archive is JSON-only.** `build_control_archive(files: Sequence[tuple[str, dict]])`
   does `json.dumps(config)` per entry (`local_executor.py:340-345`). The remote lane
   carries the same two dicts as `control_files` through
   `runner_protocol.build_execute_step_config`. Shipping a *third*, markdown, control
   file would mean touching `build_control_archive`, `local_executor`,
   `runner_protocol`, the runner agent, and the remote-side archive writer. Shipping a
   *string inside the existing agent-config dict* touches **neither the tar builder,
   the protocol, the runner agent, nor the image**. This is the whole justification
   for section 2.

4. **`/workspace/.control` is already writable by the agent uid.**
   `images/base/control/entrypoint.sh:23-26` chowns the *directory* to `lazyaf:lazyaf`
   before gosu (the `-name '*.json'` sweep at line 41 is a separate, file-level pass).
   The wrapper (uid 1000, refuses to run as root) can therefore create
   `spec_context.md` there with **no Dockerfile or entrypoint change**.

5. **`.control` is outside the commit staging area.** `_agent_repo_workdir`
   (`local_executor.py:831`) exists specifically to keep `git add -A` bounded by
   `/workspace/repo`. A bundle materialised at `/workspace/.control/spec_context.md`
   **cannot be swept into a pushed commit.** A bundle written into the checkout could.

6. **`card_id` reaches the executor two ways and the payload builder only reads one.**
   `build_agent_step_config` writes `card_id` into the step config
   (`agent_run.py:281`) *and* `start_adhoc_agent_run` writes it into
   `trigger_context` (`agent_run.py:388`), but `_attach_agent_payload` reads only
   `context.get("card_id")` (`pipeline_executor.py:2371`). A hand-written pipeline
   YAML agent step with `card_id:` in its config gets `card_id: null` on the wire
   today. This phase's resolver reads **both** (section 4.2). Pre-existing gap,
   fixed in passing because the bundle is worthless without the card id.

7. **`AcceptanceCriterion` has no ordering column** (no `order`, no `position`).
   Deterministic order = `(created_at, id)`. `UserStory.criteria` and
   `Feature.stories` relationships carry no `order_by`.

8. **`TestRef.file_path` is REPO-ROOT-relative with `/` separators** - cross-agent
   contract #3, enforced by `pytest_lazyaf._file_path` and
   `test_ingestion.normalize_repo_relative_path`. It maps onto
   `/workspace/repo/<file_path>` with no translation. It is also **only meaningful in
   the repo that declared it** - `TestRef` identity is the pair
   `(repo_id, lazyaf_test_id)`. Repo scoping of the test list is therefore a
   correctness requirement, not a nicety (section 3.4).

9. **`AcceptanceCriterion` has no `test_refs` backref.** `TestRef.criterion_id` is a
   one-way, indexed, nullable FK. Related tests are found by
   `select(TestRef).where(TestRef.criterion_id.in_(...))`, never by relationship walk.

10. **The agent-config contract test asserts key-for-attribute identity.**
    `test_every_producer_key_survives_the_file` loops every producer key and asserts
    `hasattr(loaded, key)` and equality. A new top-level producer key therefore
    *forces* a matching `AgentConfig` field in the same change. That is the pin
    (section 7.1) - it is already there, we just have to use it.

11. **`runner-common/tests/` is NOT in any tier.** `scripts/run_tier.py` selects
    `../tdd/unit`, `../tdd/demos`, `../tdd/integration` (T1), `../tdd/integration/services`
    (T2), `../tdd/e2e` (T3). `runner-common/tests/test_agent_wrapper.py` (37 tests)
    runs only if invoked by hand. **Every gated test this phase adds goes under
    `tdd/`.** Flagged to the integrator as a standing gap (section 10, note N1) - not
    this lane's to fix.

12. **`PromptTemplate` rows are CRUD-only.** `routers/spec.py` creates/reads/updates
    them; nothing resolves `PromptTemplate.content` into a step. Steps carry a raw
    `prompt_template` *string* in their config. The `{{spec_context}}` placeholder is
    therefore a property of the **renderer** (`agent_prompt.render_placeholders`),
    which serves both the raw string and any future `PromptTemplate.content`
    resolution. Wiring `PromptTemplate` rows into steps is NOT this phase.

---

## 1. What this phase is, in one paragraph

`SpecContextService` derives a markdown bundle at **dispatch time** from rows that
already exist (`Card` -> `UserStory` -> `Feature` / `AcceptanceCriterion` -> `TestRef`),
`agent_prompt` renders it into the one prompt string the backend already produces, and
`generate_agent_config` carries it in the one sidecar file the agent step already gets.
The wrapper materialises it to `/workspace/.control/spec_context.md` so the agent can
re-read it, logs one line about it, and deletes it on the way out. Nothing new is
persisted, no new channel is invented, and a card with no spec links produces a
`null` and zero bytes of prompt.

---

## 2. Delivery: the existing agent-config sidecar (decision + justification)

**DECIDED: the bundle travels as a top-level `spec_context` key inside
`/workspace/.control/agent.<step_execution_id>.json`. It is NOT a third control file.**

Justification, in the order the constraints bite:

- **Zero blast radius across owned files.** Ground truth #3: a third file means editing
  `build_control_archive` (dict -> bytes), `local_executor`'s `archive_files` list,
  `runner_protocol.build_execute_step_config`'s `control_files`, and the runner agent's
  archive writer - four files owned by the 12.6 remote lane. A key on the existing dict
  means editing the producer and the consumer, and **the remote lane gets it for free**
  because `runner_protocol` ships `generate_agent_config` output verbatim.
- **The consume-once story is already correct for this payload.** The bundle is not a
  secret, but it *is* per-step and the workspace volume outlives the step. The agent
  config is the one file already deleted on every path (`load_and_consume`, and the
  wrapper's own `finally`). A third file would need its own lifecycle rule; a key on the
  agent config inherits the right one.
- **The step config is the wrong file and was never a candidate.** `run.py` unlinks it
  in a `finally` that runs *before* the step command starts (12.3 consume-once). The
  wrapper could not read a bundle carried there. This is the exact reason the agent
  config exists as a second file at all.
- **The prompt has to carry the text anyway.** Ground truth #2: the backend renders the
  prompt. Once the bundle is in the prompt, an *additional* transport for the same bytes
  would be a second source of truth for it (R3). One key, one renderer, one file.

**The materialised file still exists**, because PLAN's contract
`test_bundle_written_to_workspace` is a real requirement and because an agent 40 turns
into a session should be able to `cat` its brief instead of trusting its own context
window. It is written **by the wrapper, from the payload**, not shipped as a file. See
section 6.

---

## 3. Bundle contents

### 3.0 The shape on the wire

`spec_context` is `None` or a dict. `None` is the clean no-op (section 5).

```jsonc
"spec_context": {
  "markdown": "## Spec Context\n\n...",   // str, never "" when the key is a dict
  "source": {                              // provenance, for forensics + 12.6.5
    "card_id": "…", "feature_id": "…", "user_story_id": "…"
  },
  "criteria_count": 3,                     // criteria PRESENT in markdown
  "test_ref_count": 2,                     // test paths PRESENT in markdown
  "estimated_tokens": 412,
  "truncated": false,
  "dropped": []                            // ordered drop-rule names actually applied
}
```

Why a dict and not a bare string: the wrapper has to log what it received (R1 - a
silently-shrunk brief is exactly the dark behaviour R1 forbids), and 12.6.5's
with/without-curation experiment needs the size and truncation facts recorded per run
without re-deriving them from prose. `dropped` is a list of stable rule names
(section 3.5), not free text.

### 3.1 Token budget

```python
SPEC_CONTEXT_MAX_TOKENS      = 4000
SPEC_CONTEXT_BYTES_PER_TOKEN = 4
SPEC_CONTEXT_MAX_BYTES       = SPEC_CONTEXT_MAX_TOKENS * SPEC_CONTEXT_BYTES_PER_TOKEN  # 16384
```

- **Budget is expressed in tokens** (the unit PLAN and the operator think in) and
  **enforced in bytes** (the unit the wire and `MAX_ARG_STRLEN` think in) - exactly the
  idiom `truncate_previous_step_logs` already uses ("Measured in UTF-8 BYTES (the wire
  unit), not characters").
- **No tokenizer.** The backend has no offline tokenizer for the target models, the
  model varies per step, and 4 bytes/token is conservative for English prose and
  source paths. `estimated_tokens = ceil(len(markdown.encode("utf-8")) / 4)`. The
  number is documented as an estimate everywhere it is surfaced.
- **Why 16 KiB and not more.** Ground truth #1. Worst-case prompt =
  template (~1 KiB) + description + spec bundle (16 KiB) + previous-step logs (32 KiB)
  + section furniture. That leaves ~80 KiB of headroom under `MAX_ARG_STRLEN` for the
  card description before an agent step starts failing with `E2BIG`. Raising the cap
  is a deliberate, measured change, not a knob to twiddle.

### 3.2 IN the bundle

Resolution starts at the card. **The linked `UserStory` is the precise link and its
parent feature is derived from `UserStory.feature_id`** - so
`test_bundle_includes_parent_feature_description` passes even when `Card.feature_id`
is null (ground truth: `UserStory.feature_id` is `nullable=False`).

| Section | Content | Source |
|---|---|---|
| Preamble | 3 lines: what this is, that it is the intent to implement, and the on-disk path | constant |
| Feature | `title`, `description`, short id | `Feature` via `UserStory.feature_id` |
| Story | `title`, full `narrative`, short id | `UserStory` |
| Acceptance criteria | every criterion of the linked story: `text`, `required` flag, `notes` when present, criterion id | `AcceptanceCriterion`, ordered `(created_at, id)` |
| Existing tests | one line per related `TestRef`: `file_path`, the criterion it covers, `lazyaf_test_id`, last observed run status | `TestRef` + latest `TestRun` |
| Footer | "Paths are relative to the repository root (`/workspace/repo`)." | constant |

**Criterion ids are included on purpose.** The integration-validation check in PLAN
("logs show agent referenced criteria by name") needs a stable token to grep for, and
an agent registering a new `lazyaf_test_id` against a criterion needs the id to put in
the marker. Cost is ~36 bytes per criterion.

**Criterion `notes` are included** - it is the field a human wrote precisely to
disambiguate the criterion - but it is the **first thing dropped** under budget
pressure (section 3.5).

**Feature-only link** (`Card.user_story_id` is null, `Card.feature_id` set): the bundle
carries the feature title + description **plus the titles and ids of its stories, one
line each, capped at `SPEC_CONTEXT_MAX_STORY_TITLES = 20`** - so the agent knows the
shape of the feature and does not reimplement a sibling story - and **no criteria and
no test refs**. Walking every story's criteria is exactly the dump this phase exists to
avoid. This is not a violation of `test_bundle_omits_unrelated_features`: sibling
stories *of the linked feature* are in scope by definition; other features are not.

**Link conflict** (`Card.feature_id` set AND `Card.user_story_id` set AND
`story.feature_id != card.feature_id`): the **story's parent wins** - the story is the
more precise link - and the service logs one WARNING naming both feature ids. The
bundle stays clean; the discrepancy is visible in the log and in the preview endpoint's
`source` block.

### 3.3 OUT of the bundle - deliberately

| Excluded | Why |
|---|---|
| **Test source code** | PLAN's open question, defaulting to paths. The agent is sitting in the checkout and can read what it decides it needs; full files would blow 16 KiB on two tests. |
| **Sibling stories' narratives and criteria** (story-linked case) | The story link is precise. Sibling detail is the noise that makes the model hedge. |
| **Other features entirely** | `test_bundle_omits_unrelated_features`. Only the derived parent feature appears. |
| **TestRun history beyond the single latest status per ref** | History belongs in the criterion-history API (12.2.6). One word per test is what changes the agent's behaviour. |
| **Other cards linked to the same story** | Sibling cards are *parallel agent work the agent cannot see*. Naming them invites duplicate implementation - the exact failure this phase claims to prevent. |
| **`TestRef`s from other repos** | Their `file_path` does not exist in this workspace. Repo-scoped by query (section 3.4). |
| **`orphan` `TestRef`s and refs with `file_path IS NULL`** | An orphan was reconciled away or auto-created before registration; the path may not exist. "Go read this file" must never be a lie. |
| **Anything from `Repo`, `Pipeline`, `StepRun`, secrets, env** | Not spec. The step config and the repo itself already carry them. |
| **`Feature.repo_ids`** | Internal scoping data, not intent. |

### 3.4 The related-test query

```python
# criteria of the linked story only
criterion_ids = [c.id for c in story.criteria]

refs = (await db.execute(
    select(TestRef)
    .where(
        TestRef.criterion_id.in_(criterion_ids),
        TestRef.repo_id == repo_id,                      # ground truth #8
        TestRef.status == TestRefStatus.ACTIVE.value,
        TestRef.file_path.is_not(None),
    )
    .order_by(TestRef.file_path, TestRef.lazyaf_test_id)  # deterministic
    .limit(SPEC_CONTEXT_MAX_TEST_REFS)                    # = 25
)).scalars().all()
```

Latest run status, **one bounded query**, no N+1 and no unbounded row set:

```python
latest = (
    select(TestRun.test_ref_id, func.max(TestRun.created_at).label("ts"))
    .where(TestRun.test_ref_id.in_(ref_ids))
    .group_by(TestRun.test_ref_id)
    .subquery()
)
rows = (await db.execute(
    select(TestRun.test_ref_id, TestRun.status)
    .join(latest, and_(TestRun.test_ref_id == latest.c.test_ref_id,
                       TestRun.created_at == latest.c.ts))
    .order_by(TestRun.test_ref_id, TestRun.id)
)).all()
```

Both legs ride `ix_test_runs_test_ref_id_created_at`. Timestamp ties yield duplicate
rows; the service keeps the **first** per `test_ref_id` under the `(test_ref_id, id)`
order, which is deterministic. A ref with no runs renders `last run: never`, never a
blank.

**R5**: the story/feature/criteria load is a single
`select(UserStory).options(selectinload(UserStory.criteria), selectinload(UserStory.feature))`;
the feature-only path is
`select(Feature).options(selectinload(Feature.stories))`. Every relationship the
service touches is eager-loaded. No lazy attribute access after the await.

### 3.5 Truncation: what gets dropped first

The bundle is assembled as an ordered list of blocks, then reduced until
`len(markdown.encode("utf-8")) <= SPEC_CONTEXT_MAX_BYTES`. Rules fire **in this order**,
each one re-measuring before the next:

| # | Rule name (`dropped` entry) | What goes |
|---|---|---|
| 1 | `criterion_notes` | Every criterion `notes` field. Supplementary by construction. |
| 2 | `feature_description` | The feature body. The feature **title** always survives. |
| 3 | `story_narrative` | **Tail**-truncated to fit, keeping the head, with an inline marker. Deliberately the opposite of `truncate_previous_step_logs` (which keeps the tail): a narrative states its intent in its opening lines; a log states its outcome in its last ones. |
| 4 | `story_titles` | Feature-only path: the sibling story list. |
| 5 | `test_refs` | Test lines beyond the first 10, then all of them. Paths are cheap (~80 bytes) so this rarely fires. |
| 6 | `optional_criteria` | Criteria with `required=False`, all of them, replaced by one count line. |
| 7 | `required_criteria` | **Last resort.** Trailing required criteria, replaced by `[...N further required criteria omitted - the spec exceeds the context budget...]`. |
| 8 | `hard_clamp` | If the markdown is *still* over after rule 7 (a single pathological criterion), clamp to `SPEC_CONTEXT_MAX_BYTES` on a UTF-8 boundary with a trailing marker. Guarantees the byte cap is a fact, not an intention. |

**Required criteria are never all dropped** (rules 7 and 8 leave at least the first one
plus the marker). They are the contract; a bundle without them is worse than no bundle.

**Every applied rule leaves a visible marker in the markdown** *and* appends its name to
`dropped` *and* sets `truncated: true`. R1: an agent reading a shrunk brief must be able
to see that it is shrunk, and so must the operator reading the step log.

Marker constant:

```python
SPEC_CONTEXT_TRUNCATION_MARKER = (
    "> [spec context truncated to fit the {tokens}-token budget: {what}]\n"
)
```

### 3.6 Rendered shape (this is the format; implement it literally)

```markdown
## Spec Context

This is the curated slice of the product spec for the card you are working on.
It is the intent you are implementing - satisfy the acceptance criteria below.
The same text is on disk at /workspace/.control/spec_context.md.

### Feature: Per-repo API rate limiting  (feature 4f2a1c9e)
Protect the public API from runaway clients without penalising normal use.

### Story: Operator sets a per-repo request budget  (story 91bc77d0)
As an operator I want to cap requests per repo per minute so that one
misbehaving integration cannot starve the others.

### Acceptance criteria (3)
- [required] (criterion a11b3f42) A repo over its budget receives HTTP 429.
  note: the budget is per minute, not per hour.
- [required] (criterion c22d90ab) The 429 body names the retry-after seconds.
- [optional] (criterion e33f0c17) Rate-limit headers are emitted on every response.

### Existing tests for these criteria (2)
Read these before writing new ones - they already cover the criteria named.
- tests/api/test_rate_limit.py  (criterion a11b3f42, lazyaf_test_id "rl-429", last run: passed)
- tests/api/test_rate_limit.py  (criterion c22d90ab, lazyaf_test_id "rl-retry-after", last run: failed)

Paths are relative to the repository root (/workspace/repo).
```

Short ids are the first 8 characters of the uuid, matching the logging idiom already
used throughout `agent_run.py` (`pipeline_run.id[:8]`). Full ids live in `source` and
in the preview endpoint.

---

## 4. Assembly and dispatch

### 4.1 `backend/app/services/spec_context.py` (NEW)

```python
async def build_spec_context(
    db: AsyncSession, *, card_id: str | None, repo_id: str
) -> dict | None:
    """Assemble the curated spec bundle for one card, or None.

    None (never {} and never a bundle with empty markdown) is the clean
    no-op: no card, no spec links, or nothing survived resolution.
    """
```

- Module-level async function, not a class. There is no state to hold and the codebase's
  service idiom (`test_ingestion.py`, `agent_prompt.py`) is module-level functions.
  (Deviation D5 from PLAN's `SpecContextService.build_bundle(card_id) -> str`.)
- `repo_id` is a required argument, not derived from the card: the **run's** repo is
  what the workspace is checked out at, and test paths are only valid there. If
  `card.repo_id != repo_id` the service logs one WARNING and uses the passed
  `repo_id` - the workspace is the authority.
- Constants `SPEC_CONTEXT_MAX_TOKENS`, `SPEC_CONTEXT_BYTES_PER_TOKEN`,
  `SPEC_CONTEXT_MAX_BYTES`, `SPEC_CONTEXT_MAX_TEST_REFS`,
  `SPEC_CONTEXT_MAX_STORY_TITLES` and `SPEC_CONTEXT_TRUNCATION_MARKER` are **defined in
  `control_layer/workspace.py`** (which has no DB or docker imports and is the file the
  dormant contract suite imports standalone) and **imported from there** by this
  service. Rationale: the producer must be able to re-assert the cap without importing
  the DB layer, and `PREVIOUS_STEP_LOGS_MAX_BYTES` already lives there for the same
  reason. One definition, two users.

### 4.2 `pipeline_executor._attach_agent_payload` (~15 line insert)

Immediately after the existing `capped_logs` line and **before** `render_agent_prompt`:

```python
from app.services.spec_context import build_spec_context

# Curation is on by default and switchable per step: `spec_context: false`
# is the 12.6.5 A/B lever (with-curation vs without) and the escape hatch
# for a step whose card is linked to a spec it must not read.
card_id = step_config.get("card_id") or context.get("card_id")   # ground truth #6
spec_context = None
if step_config.get("spec_context", True) and card_id:
    spec_context = await build_spec_context(db, card_id=card_id, repo_id=repo.id)

prompt = render_agent_prompt(
    card_title=card_title,
    card_description=card_description,
    prompt_template=step_config.get("prompt_template"),
    previous_step_logs=capped_logs,
    spec_context=(spec_context or {}).get("markdown"),
)
```

and in the `exec_config["agent"] = {...}` dict, replace
`"card_id": context.get("card_id"),` with `"card_id": card_id,` and add
`"spec_context": spec_context,`.

- **No new dispatch path.** Both lanes reach `generate_agent_config(**agent_payload)`
  (local: `local_executor.py:861`; remote: `pipeline_executor.py:2769`), so the remote
  runner gets the bundle with **zero** changes to `remote_executor.py`,
  `runner_protocol.py`, or the runner agent.
- **Why the switch defaults to `True` and is not reserved.** `spec_context` is
  deliberately absent from `agent_run._RESERVED_STEP_CONFIG_KEYS`, so a caller can pass
  it through `extra_config` / `start_card_work(step_config=...)` without touching
  `agent_run.py` at all. `agent_run.py` needs **no edit in this phase.**
- **Disabled vs no-links is distinguished in the LOG, not on the wire.** The wire says
  `null` in both cases, truthfully. `_attach_agent_payload` logs
  `spec context disabled by step config for step %s` when the switch is off. The variant
  identity for a 12.6.5 experiment lives in the experiment's step config, which 12.6.5
  already records - not in a second, redundant wire field.

### 4.3 `agent_prompt.py` - the `{{spec_context}}` placeholder

```python
PLACEHOLDERS = ("{{title}}", "{{description}}", "{{spec_context}}")

SPEC_CONTEXT_PLACEHOLDER = "{{spec_context}}"

def render_placeholders(template, title, description, spec_context: str = "") -> str: ...

def render_agent_prompt(*, card_title="", card_description="",
                        prompt_template=None, previous_step_logs=None,
                        spec_context: Optional[str] = None) -> str: ...
```

Semantics, exactly:

1. **Double braces, not single.** `{{spec_context}}`, matching the frozen vocabulary
   `{{title}}` / `{{description}}`. PLAN writes `{spec_context}`; a single-brace
   placeholder in the same template string as double-brace ones is a trap for template
   authors and for the "plain string replacement, no format spec" rule that keeps user
   content from reaching into the process. (Deviation D1.)
2. **Placeholder present** -> the bundle is substituted *there*; the author controls
   placement. The bundle markdown is **self-contained** (it carries its own
   `## Spec Context` heading), so a bare placeholder on its own line is the whole usage.
3. **Placeholder absent and a bundle exists** -> the bundle is appended as a section
   **after the template body and before `## Previous Step Output`**. Intent first,
   transient step output last, so the prompt's final instruction stays
   *"Use this context when completing the current task."*
4. **Bundle empty/None** -> substitute the empty string, and collapse the placeholder's
   own line: replace `"{{spec_context}}\n"` before `"{{spec_context}}"` so the template
   does not grow a blank line. No section is appended. **This is the clean no-op.**
   Documented guidance in the docstring: put the placeholder alone on its own line; do
   not wrap it in a heading you would not want to see empty.
5. **Detection of case 2 vs 3 happens BEFORE substitution** (`SPEC_CONTEXT_PLACEHOLDER
   in template`), so a bundle that happens to contain the literal placeholder text
   cannot re-trigger the append.

### 4.4 `control_layer/workspace.py` - the producer

- New constants (section 4.1) plus:
  ```python
  SPEC_CONTEXT_DIR      = "/workspace/.control"   # mirrors local_executor.CONTROL_CONFIG_DIR
  SPEC_CONTEXT_FILENAME = "spec_context.md"
  SPEC_CONTEXT_PATH     = f"{SPEC_CONTEXT_DIR}/{SPEC_CONTEXT_FILENAME}"
  ```
  Duplicated rather than imported because `workspace.py` must stay importable with no
  docker/config dependency (the dormant contract suite imports it standalone). Drift is
  impossible because the contract test pins
  `SPEC_CONTEXT_DIR == f"/workspace/{local_executor.CONTROL_CONFIG_DIR}"` and
  `workspace.SPEC_CONTEXT_FILENAME == agent_config.SPEC_CONTEXT_FILENAME` (section 7.1).
  This is exactly how `AGENT_CONFIG_VERSION` is already handled on both sides.
- `generate_agent_config(..., spec_context: Optional[Dict[str, Any]] = None)`, emitted
  verbatim as the top-level `"spec_context"` key, and added to `agent_config_keys()`.
- **Producer-side enforcement, loud, at dispatch:**
  ```python
  if spec_context is not None:
      markdown = spec_context.get("markdown")
      if not isinstance(markdown, str) or not markdown:
          raise ValueError("spec_context must carry non-empty 'markdown', or be None")
      size = len(markdown.encode("utf-8"))
      if size > SPEC_CONTEXT_MAX_BYTES:
          raise ValueError(
              f"spec_context is {size} bytes, over the "
              f"{SPEC_CONTEXT_MAX_BYTES}-byte ({SPEC_CONTEXT_MAX_TOKENS}-token) "
              "budget; the assembler must truncate before dispatch"
          )
  ```
  The producer is the last gate before the wire and it already raises on an unknown
  agent. A bundle that slipped the assembler's truncation must fail the step at
  dispatch with a named number, not arrive oversized and kill the CLI with `E2BIG`
  twenty minutes later.

---

## 5. A card with NO spec links

The whole path is a `None`, checked once:

| Situation | Result |
|---|---|
| No `card_id` (playground, ad-hoc, YAML step without one) | `build_spec_context` not called; `spec_context = None` |
| Card row missing | `None`, one WARNING naming the id |
| `Card.user_story_id` and `Card.feature_id` both null | `None` |
| Story/feature id set but the row is gone | `None`, one WARNING |
| `spec_context: false` in step config | `None`, one INFO naming the step |
| Story linked but zero criteria and zero test refs | **Not** `None` - feature + story + narrative is real intent. Renders the criteria section as `### Acceptance criteria (0)` followed by one line: `No acceptance criteria have been written for this story yet.` |

Downstream of a `None`:

- `render_agent_prompt` appends **nothing** and substitutes the placeholder with the
  empty string plus line collapse. **The prompt is byte-identical to today's.** This is
  pinned by a test (section 7.2, `test_no_links_prompt_is_byte_identical`).
- `generate_agent_config` emits `"spec_context": null`.
- The consumer loads `spec_context=None`; `has_spec_context` is `False`.
- **The wrapper writes no file** and logs exactly one line:
  `[agent] spec context: none (no spec links for this card)`.
  One line, because a *silent* absence is indistinguishable from a bug that dropped the
  bundle (R1). One line is not noise; an empty `## Spec Context` heading would be.

---

## 6. The consumer side

### 6.1 `runner_common/agent_config.py`

```python
SPEC_CONTEXT_FILENAME = "spec_context.md"   # pinned == workspace.SPEC_CONTEXT_FILENAME

@dataclass
class AgentConfig:
    ...
    spec_context: Optional[Dict[str, Any]] = None
    """The curated spec bundle (12.6.6) or None. `markdown` is already in
    `prompt`; this carries the same text for materialisation plus the size
    and truncation facts the wrapper logs."""

    @property
    def spec_markdown(self) -> Optional[str]:
        return (self.spec_context or {}).get("markdown") or None

    @property
    def has_spec_context(self) -> bool:
        return bool(self.spec_markdown)
```

Loader rules, in the module's existing style (print a reason, return `None`, never
raise, never silently coerce):

- key absent -> `None`. Backward compatible with a pre-12.6.6 backend.
- present and not a dict (and not `null`) -> `_fail("spec_context must be an object or null; got <type>")`, return `None`.
- present, a dict, with a non-string non-null `markdown` -> `_fail(...)`, return `None`.
- **No version bump.** `AGENT_CONFIG_VERSION` stays `1`: an additive, optional key that
  an older consumer ignores and a newer consumer defaults is exactly what does *not*
  justify a bump. Bumping would strand every runner agent in the field mid-phase.

### 6.2 `runner_common/agent_wrapper.py`

New helper, called in `main()` after `_install_sigterm_handler()` and before the
executor is built:

```python
def _write_spec_context(cfg: AgentConfig, control_dir: Path) -> Optional[Path]:
    """Materialise the curated bundle next to the agent config. Never fatal."""
```

- **Path is derived, not taken from the payload**: `control_dir` is
  `config_path.parent` - the directory the backend already announced through
  `LAZYAF_AGENT_CONFIG_PATH`. The wrapper never writes to a path a payload told it to.
- Written with `os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)`, matching the tar's
  0600 file mode. The directory is already `lazyaf`-owned (ground truth #4).
- No bundle -> log the one-line "none" message (section 5), return `None`.
- Bundle present -> log exactly:
  `[agent] spec context: N criteria, M related tests, ~T tokens, truncated=<bool> -> <path>`
  and, when `truncated`, a second line naming `dropped`:
  `[agent] note: spec context was truncated (dropped: criterion_notes, feature_description)`
  - the same shape as the existing `previous_step_logs_truncated` note.
- `OSError` -> **WARNING to stderr naming the path and the errno, and continue.** Not a
  step failure: the bundle is already in the prompt, and killing a real agent run over a
  convenience file would be a worse outcome than the file's absence. The warning is the
  loudness R1 asks for; the silent version would be the violation.
- **Deleted in the existing `finally`** via `unlink_quietly` - the workspace volume is
  shared across the run's steps, and step N+1's agent must never read step N's brief.

The wrapper **does not touch `cfg.prompt` and does not add a system-prompt flag.**
Ground truth #2: the backend is the single renderer. `ExecutorConfig` gains no field,
`claude.py` / `gemini.py` / `mock.py` are **not edited in this phase.**

---

## 7. Contract tests

### 7.1 The producer<->consumer pin (extends the existing cross-agent contract #1)

`tdd/unit/control_runtime/test_agent_config_contract.py` - **extend, do not fork.**

| Test | Pins |
|---|---|
| *(existing)* `test_every_producer_key_survives_the_file` | Add a populated `spec_context` dict to `_producer_payload()`. The existing loop then forces `AgentConfig.spec_context` to exist and to survive the real JSON file with the value intact. **This is the pin.** |
| *(existing)* `test_consumer_keys_superset_of_producer_keys` | Passes only once the dataclass field exists. |
| `test_spec_context_filename_pinned_on_both_sides` | `workspace.SPEC_CONTEXT_FILENAME == agent_config.SPEC_CONTEXT_FILENAME` |
| `test_spec_context_dir_matches_the_control_dir` | `workspace.SPEC_CONTEXT_DIR == f"/workspace/{local_executor.CONTROL_CONFIG_DIR}"` |
| `test_spec_context_absent_loads_as_none` | Pre-12.6.6 payload -> `spec_context is None`, `has_spec_context is False`, exit path unaffected |
| `test_spec_context_null_loads_as_none` | Explicit `null` behaves identically |
| `test_non_dict_spec_context_is_refused` | `"spec_context": "text"` -> `load_agent_config` returns `None` and prints the reason |
| `test_oversized_spec_context_is_refused_at_dispatch` | `generate_agent_config` raises `ValueError` naming the byte and token budget |
| `test_spec_context_carries_no_secrets` | The existing JWT/API-key scan covers the new key (it scans the serialised file) |

### 7.2 New files

**`tdd/unit/services/test_spec_context_bundle.py`** - PLAN's `test_context_bundle_assembly.py`.
Real async session, real rows, no mocks.

| Test | Contract |
|---|---|
| `test_card_with_story_link_pulls_narrative` | Full narrative present |
| `test_bundle_includes_all_criteria` | Every criterion of the linked story, in `(created_at, id)` order |
| `test_bundle_includes_related_test_paths` | `TestRef.file_path` surfaced verbatim, repo-root-relative |
| `test_bundle_includes_last_run_status_per_test` | Latest `TestRun.status` per ref; `never` when there are none |
| `test_bundle_includes_parent_feature_description` | Feature derived from `story.feature_id` even when `card.feature_id` is null |
| `test_bundle_omits_unrelated_features` | Sibling feature's story/criteria/tests absent |
| `test_bundle_omits_test_refs_from_other_repos` | A ref on the same criterion in another repo is excluded (ground truth #8) |
| `test_bundle_omits_orphan_and_pathless_test_refs` | status `orphan` / `file_path is None` excluded |
| `test_bundle_handles_card_without_links` | Returns `None`, no exception |
| `test_bundle_handles_missing_card` | Returns `None`, warning logged |
| `test_feature_only_link_lists_story_titles_not_criteria` | Feature-only path |
| `test_story_link_beats_conflicting_feature_link` | Story's parent wins, warning logged |
| `test_bundle_size_capped` | Oversized spec -> `len(markdown.encode()) <= SPEC_CONTEXT_MAX_BYTES` |
| `test_truncation_drops_notes_before_criteria` | Drop order, rule 1 before rule 6 |
| `test_truncation_never_drops_every_required_criterion` | Rule 7/8 floor |
| `test_truncation_is_announced_in_markdown_and_metadata` | Marker + `truncated: True` + `dropped` names (R1) |
| `test_story_narrative_is_head_kept_on_truncation` | Opposite of the log rule, on purpose |
| `test_estimated_tokens_matches_the_byte_heuristic` | `ceil(bytes/4)` |
| `test_no_lazy_loads_after_await` | Bundle built from an expired session with `expire_on_commit` semantics - fails loudly if a relationship was not `selectinload`ed (R5) |

**`tdd/unit/services/test_agent_prompt.py`** - **extend** (12.5's file, additive only).

| Test | Contract |
|---|---|
| `test_prompt_template_can_reference_spec_context` | `{{spec_context}}` resolves in place |
| `test_bundle_appended_when_template_omits_the_placeholder` | Appended after the body, **before** `## Previous Step Output` |
| `test_placeholder_with_no_bundle_collapses_to_nothing` | No blank line, no heading |
| `test_no_links_prompt_is_byte_identical` | With `spec_context=None`, output equals the pre-12.6.6 renderer's output byte for byte |
| `test_spec_context_is_plain_replacement` | A bundle containing `{` / `}` / `{{title}}` is not re-interpreted |

**`tdd/unit/control_runtime/test_spec_context_injection.py`** - PLAN's
`test_context_injection.py`, wrapper side. Drives the **real** `agent_wrapper.main()`
over real files, in the style of `runner-common/tests/test_agent_wrapper.py`'s
`agent_env` fixture (lift the fixture into this module or the package conftest -
**do not add tests to `runner-common/tests/`, it is outside every tier**, note N1).

| Test | Contract |
|---|---|
| `test_bundle_written_to_workspace` | File exists at `<control_dir>/spec_context.md` while the executor runs, content == `markdown` |
| `test_bundle_file_is_deleted_after_the_step` | Gone after `main()` returns - consume-once on a shared volume |
| `test_bundle_file_mode_is_0600` | Skipped on Windows via the existing platform idiom, not a bare skip |
| `test_no_bundle_writes_no_file_and_logs_one_line` | The clean no-op |
| `test_executor_receives_the_bundle_in_its_prompt` | `RecordingExecutor` sees the markdown inside `ExecutorConfig.prompt` (PLAN's `test_executor_includes_in_prompt`, relocated to where the single renderer actually lives) |
| `test_unwritable_control_dir_warns_and_the_step_still_runs` | Exit code unchanged, WARNING on stderr |
| `test_truncated_bundle_logs_the_dropped_rules` | R1 |

**`tdd/integration/api/test_spec_context_api.py`** - the preview endpoint (section 8.1).

| Test | Contract |
|---|---|
| `test_preview_returns_bundle_for_linked_card` | 200, markdown + metadata |
| `test_preview_returns_null_markdown_for_unlinked_card` | 200 with `markdown: null` - **not** 404; "this card has no spec context" is a successful answer |
| `test_preview_404s_on_unknown_card` | 404 |
| `test_preview_matches_what_dispatch_would_send` | Same card -> the endpoint's markdown is byte-identical to `build_spec_context(...)["markdown"]`. The preview cannot drift from the dispatched bundle. |

**`tdd/unit/services/test_agent_step_dispatch.py`** - **extend** (12.5's file).

| Test | Contract |
|---|---|
| `test_agent_payload_carries_the_spec_context` | Linked card -> `exec_config["agent"]["spec_context"]["markdown"]` non-empty |
| `test_spec_context_false_disables_curation` | `spec_context: false` -> `None` on the wire, prompt unchanged |
| `test_card_id_is_read_from_step_config_or_trigger_context` | Ground truth #6 |

---

## 8. Migration: NONE - **0008 is not used**

Everything the bundle needs exists at head `0006`:

| Needed | Column | Landed in |
|---|---|---|
| card -> story | `cards.user_story_id` | 12.2.5 |
| card -> feature | `cards.feature_id` | 12.2.5 |
| story -> feature | `user_stories.feature_id` | 12.2.5 |
| criteria | `acceptance_criteria.*` incl. `required`, `notes` | 12.2.5 |
| test paths | `test_refs.file_path`, `.criterion_id`, `.repo_id`, `.status` | 12.2.6 |
| last status | `test_runs.status`, `.created_at` + `ix_test_runs_test_ref_id_created_at` | 12.2.6 |

The bundle is **derived at dispatch time and stored nowhere**. It is reproducible from
the spec tables plus the run's repo id, and the run's own record of what it sent is the
step log line plus the (already retained) prompt. Persisting it would create a second
source of truth for the spec that goes stale the moment a criterion is edited.

**Lane 12.6.6 therefore claims no migration number. 0008 is released back to the pool -
the integrator should note it as available rather than leaving a hole in the sequence.**

### 8.1 Preview endpoint (the one new API surface)

`GET /api/cards/{card_id}/spec-context` -> `SpecContextRead`:

```json
{"card_id":"…","markdown":"…|null","source":{"feature_id":"…","user_story_id":"…"},
 "criteria_count":3,"test_ref_count":2,"estimated_tokens":412,
 "truncated":false,"dropped":[],
 "budget_tokens":4000,"budget_bytes":16384}
```

Read-only, no side effects, `repo_id` taken from the card. It exists for two reasons,
both load-bearing:

- **R1.** A curated brief that only becomes visible by reading a container's stdout
  after burning a run is dark. This is the "look before you spend" surface.
- **The exit gate.** "One experiment comparing with/without curation" requires a human
  to be able to see *what* was curated when a variant underperforms.

`test_preview_matches_what_dispatch_would_send` is what stops it becoming a second,
drifting assembler.

**No UI surface ships in this phase, so R8 does not apply.** Stated, not silently
skipped: the endpoint is an operator/API surface. If the integrator wants it in the card
panel, that is a follow-on with its own Playwright spec.

---

## 9. Deviations from PLAN (owner veto welcome)

| # | PLAN says | This design | Why |
|---|---|---|---|
| D1 | `{spec_context}` placeholder | `{{spec_context}}` | `agent_prompt.PLACEHOLDERS` is a **frozen** double-brace vocabulary shared with the legacy renderer. Mixing brace styles in one template is a trap and invites a `str.format` implementation, which would let user template content reach into the process. |
| D2 | "Claude/Gemini wrappers prepend spec_context.md to system prompt" | Backend renders it into the one prompt; wrapper only materialises the file | 12.5 moved rendering backend-side *for this phase* (`agent_prompt.py` docstring names the 12.6.6 bundle explicitly). Prepending in the wrapper would be a second producer of the most important string in the system (R3), duplicated across two CLI dialects, and would bypass the token budget the backend enforces. Neither CLI has a system-prompt seam in `ExecutorConfig` today; adding one to serve text the prompt already carries is cost with no benefit. |
| D3 | Bundle at `/workspace/.control/spec_context.md` "via the control layer" | Carried in the agent-config sidecar; **materialised at that exact path by the wrapper** | The observable contract (`test_bundle_written_to_workspace`) is met exactly. The transport changes because the control archive is JSON-only and a third file costs four files owned by other lanes (ground truth #3). |
| D4 | Implicit migration 0008 | No migration | Section 8. |
| D5 | `SpecContextService.build_bundle(card_id) -> str` | `async def build_spec_context(db, *, card_id, repo_id) -> dict \| None` | `repo_id` is required for correctness (test paths are repo-scoped); the metadata is needed by the wrapper log, the preview endpoint and 12.6.5; module-level functions are the codebase's service idiom. |
| D6 | Open question: test source code or paths? | **Paths only** | PLAN's own default. Full files would spend the entire 16 KiB budget on two tests, and the agent is sitting in the checkout. |
| D7 | (not in PLAN) | Per-step `spec_context: true\|false` switch | The exit gate requires a with/without experiment; without a switch there is no "without". |

---

## 10. File ownership - ONE implementer

**OWNS (edit freely):**

| File | Change |
|---|---|
| `backend/app/services/spec_context.py` | **NEW** - assembly + truncation |
| `backend/app/schemas/spec_context.py` | **NEW** - `SpecContextRead` |
| `backend/app/routers/spec_context.py` | **NEW** - the preview endpoint |
| `backend/app/services/control_layer/workspace.py` | constants, `spec_context` kwarg, `agent_config_keys()`, budget enforcement |
| `backend/app/services/control_layer/__init__.py` | re-export the new constants |
| `backend/app/services/agent_prompt.py` | `{{spec_context}}` placeholder + section |
| `backend/app/services/pipeline_executor.py` | **`_attach_agent_payload` only** (~15 lines, section 4.2) |
| `runner-common/runner_common/agent_config.py` | `SPEC_CONTEXT_FILENAME`, `AgentConfig.spec_context` + properties, loader validation |
| `runner-common/runner_common/agent_wrapper.py` | `_write_spec_context`, the log lines, the `finally` unlink |
| `tdd/unit/services/test_spec_context_bundle.py` | **NEW** |
| `tdd/unit/control_runtime/test_spec_context_injection.py` | **NEW** |
| `tdd/integration/api/test_spec_context_api.py` | **NEW** |
| `tdd/unit/control_runtime/test_agent_config_contract.py` | **extend** (section 7.1) |
| `tdd/unit/services/test_agent_prompt.py` | **extend** (section 7.2) |
| `tdd/unit/services/test_agent_step_dispatch.py` | **extend** (section 7.2) |

**MUST NOT TOUCH (requested edits go in the report):**
`backend/app/main.py`, `backend/app/models/__init__.py`, `frontend/src/App.svelte`,
`backend/app/models/spec.py`, `backend/app/models/testref.py`, `backend/app/models/card.py`
(no schema change), `backend/app/services/agent_run.py` (the switch rides `extra_config`),
`backend/app/services/execution/*` (the sidecar design exists to avoid them),
`backend/app/routers/spec.py`, `backend/app/routers/test_results.py`,
`runner-common/runner_common/executors/*`, `images/base/*`,
`backend/alembic/versions/*`, `scripts/*`, anything under `tdd/qa/`,
`frontend/e2e/qa/`, `upcoming/qa-*.md`.

**Notes for the integrator:**

- **N1.** `runner-common/tests/` (two files, ~50 tests incl. `test_agent_wrapper.py`) is
  selected by no tier in `scripts/run_tier.py`. Those tests are effectively ungated.
  Out of scope here - every test this phase adds is under `tdd/` - but it is a real R4
  ratchet gap worth a wave of its own.
- **N2.** Migration **0008 is unused and available**; do not leave it reserved.

---

## 11. Exact registration lines

`backend/app/main.py`, in the import block alongside the other routers:

```python
from app.routers import spec_context
```

`backend/app/main.py`, with the other `include_router` calls (immediately after the
existing `app.include_router(test_results.router)` at line 149):

```python
app.include_router(spec_context.router)
```

Nothing else. **No `backend/app/models/__init__.py` change** (no new models),
**no `frontend/src/App.svelte` change** (no UI surface), **no alembic revision.**

---

## 12. Exit gate for this phase

1. `python scripts/run_tier.py T1` green, with the new counts.
2. `tdd/unit/services/test_spec_context_bundle.py` green (19 tests as specified).
3. `tdd/unit/control_runtime/test_spec_context_injection.py` green (7 tests).
4. `tdd/unit/control_runtime/test_agent_config_contract.py` green with the extended
   `_producer_payload` - this is the producer<->consumer pin.
5. `tdd/integration/api/test_spec_context_api.py` green (4 tests).
6. A dogfood-style agent run against a card linked to a story with criteria and at
   least one `TestRef`: the step log carries the
   `[agent] spec context: N criteria, M related tests, ~T tokens` line, and
   `/workspace/.control/spec_context.md` is absent after the step (consume-once).
7. Handed to 12.6.5: one experiment with two variants differing only in
   `spec_context: true|false`, reported per-variant on linked-card pass rate.

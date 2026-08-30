"""
Agent prompt rendering - Phase 12.5.

The prompt is the most important string in the system, so it gets exactly
ONE producer (R3). Before 12.5 the container rendered it
(`runner_common.entrypoint.build_prompt`); on the control path the BACKEND
renders it and ships the finished text in the agent config file.

Why the move:
- The backend already owns `PromptTemplate`, the card fields, the resolved
  AgentFile definitions and (at 12.6.6) the spec bundle. A container that
  re-templates is a second source of truth for that string.
- The container has no DB, so anything DB-sourced would have to travel on
  the wire anyway - at which point rendering there buys nothing.

Compatibility with the legacy renderer is deliberate and bounded:
- the SAME placeholder vocabulary (`{{title}}`, `{{description}}`),
- the SAME default template body,
- the SAME `## Previous Step Output` section.

One deliberate DROP on the control path: the README-scraping branch of the
legacy default prompt. The agent can read the repository itself (it is
sitting in the workspace), the scrape was a fixed 2000-byte head of one of
four filenames, and 12.6.6 replaces that slot with curated spec context.

`runner_common.entrypoint.build_prompt` stays untouched for the legacy path
and is named in the 12.6 deletion list. `tdd/unit/services/test_agent_prompt.py`
pins the placeholder vocabulary on this side so the two cannot drift on the
part that matters.
"""
import re
from typing import Optional

#: The placeholder vocabulary. Frozen: `{{title}}` and `{{description}}` are
#: the ONE thing the legacy renderer and this one had to agree on while both
#: existed (12.5 -> 12.6). `{{spec_context}}` joined them at 12.6.6, after the
#: legacy renderer was already deleted - it is a control-path-only slot, and
#: it is deliberately DOUBLE-braced so one template never mixes brace styles
#: (a single-brace placeholder next to double-brace ones invites a
#: `str.format` implementation, which would let user template content reach
#: into the process).
PLACEHOLDERS = ("{{title}}", "{{description}}", "{{spec_context}}")

#: The 12.6.6 curated-spec slot. Named so callers can test for it rather than
#: re-spelling the literal.
SPEC_CONTEXT_PLACEHOLDER = "{{spec_context}}"

#: ONE pass over the frozen vocabulary. A single pass is the whole security
#: property: substituted content is never re-scanned, so neither a card
#: description nor a spec bundle can smuggle in another placeholder.
_PLACEHOLDER_RE = re.compile(r"\{\{(title|description|spec_context)\}\}")

#: The default prompt when a step declares no `prompt_template`. Byte-identical
#: to the legacy default MINUS the README scrape (see module docstring), PLUS
#: the `{{spec_context}}` slot that replaced it. With no bundle the slot and
#: its own newline collapse to nothing, so the rendered default is still
#: byte-identical to the pre-12.6.6 one.
DEFAULT_PROMPT_TEMPLATE = """You are implementing a feature for this project.

## Feature Request
Title: {{title}}

Description:
{{description}}

{{spec_context}}
## Instructions
1. Implement this feature following existing code patterns
2. Write tests if a test framework is present
3. Commit your changes with a clear message
4. Do not modify unrelated code
5. Keep changes minimal and focused
"""

PREVIOUS_STEP_SECTION = """

## Previous Step Output
The previous pipeline step produced the following output:
```
{logs}
```

Use this context when completing the current task.
"""


def render_placeholders(
    template: str,
    title: str,
    description: str,
    spec_context: str = "",
) -> str:
    """Substitute the frozen placeholder vocabulary into a template.

    Plain substitution, exactly like the legacy renderer: no format spec, no
    expression evaluation - a prompt template is user content and must never
    be able to reach into the process.

    ONE pass (12.6.6). The legacy renderer chained two `str.replace` calls,
    which meant a value substituted early was re-scanned by the next call. A
    third slot carrying DB-sourced spec text made that ordering load-bearing,
    so the three are now substituted simultaneously and nothing that comes
    OUT of a substitution can be substituted again.

    An empty/absent `spec_context` additionally collapses the placeholder's
    own line, so a template that reserves a line for the bundle does not grow
    a blank one when there is no bundle. This is what keeps the rendered
    default prompt byte-identical to the pre-12.6.6 one.
    """
    values = {
        "title": title or "",
        "description": description or "",
        "spec_context": spec_context or "",
    }
    if not values["spec_context"]:
        template = template.replace(SPEC_CONTEXT_PLACEHOLDER + "\n", "")
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template)


def render_agent_prompt(
    *,
    card_title: str = "",
    card_description: str = "",
    prompt_template: Optional[str] = None,
    previous_step_logs: Optional[str] = None,
    spec_context: Optional[str] = None,
) -> str:
    """Render the full prompt shipped in the agent config file.

    Args:
        card_title: `{{title}}`.
        card_description: `{{description}}`.
        prompt_template: The step's own template; falsy means
            DEFAULT_PROMPT_TEMPLATE.
        previous_step_logs: Already capped/truncated by the caller
            (`control_layer.workspace.truncate_previous_step_logs`); appended
            verbatim as the `## Previous Step Output` section when present.
        spec_context: The MARKDOWN of the 12.6.6 curated spec bundle
            (`spec_context.build_spec_context(...)["markdown"]`), or None when
            the card has no spec links / curation is off for the step.

    `spec_context` semantics, exactly:

    1. **Placeholder present in the template** -> the bundle is substituted
       THERE and nothing is appended; the template author controls placement.
       The bundle is self-contained (it carries its own `## Spec Context`
       heading), so a bare `{{spec_context}}` on its own line is the whole
       usage. Do not wrap it in a heading you would not want to see empty.
    2. **Placeholder absent and a bundle exists** -> the bundle is appended
       after the template body and BEFORE `## Previous Step Output`. Intent
       first, transient step output last, so the prompt's final instruction
       stays "Use this context when completing the current task."
    3. **No bundle** -> the placeholder and its own newline collapse to
       nothing and no section is appended. The prompt is byte-identical to the
       pre-12.6.6 one. This is the clean no-op: `None` is the ONE spelling of
       "this card has no spec context", never an empty `## Spec Context`
       heading.

    Case 1-vs-2 is decided on the RAW template, before any substitution, so a
    bundle that happens to contain the literal `{{spec_context}}` text cannot
    re-trigger the append.

    Returns:
        The prompt text. Never None, never empty when a template exists.
    """
    template = prompt_template or DEFAULT_PROMPT_TEMPLATE
    placeholder_declared = SPEC_CONTEXT_PLACEHOLDER in template

    prompt = render_placeholders(
        template, card_title, card_description, spec_context
    )

    if spec_context and not placeholder_declared:
        # Exactly one blank line between the body and the bundle, and the
        # bundle's own trailing newline dropped: PREVIOUS_STEP_SECTION opens
        # with its own blank line, and the frozen shape of that section must
        # not grow a second one just because a bundle was appended first.
        prompt = prompt.rstrip("\n") + "\n\n" + spec_context.rstrip("\n")
        if not previous_step_logs:
            prompt += "\n"

    if previous_step_logs:
        prompt += PREVIOUS_STEP_SECTION.format(logs=previous_step_logs)

    return prompt

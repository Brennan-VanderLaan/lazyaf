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
from typing import Optional

#: The placeholder vocabulary. Frozen: it is the ONE thing the legacy
#: renderer and this one must agree on while both exist (12.5 -> 12.6).
PLACEHOLDERS = ("{{title}}", "{{description}}")

#: The default prompt when a step declares no `prompt_template`. Byte-identical
#: to the legacy default MINUS the README scrape (see module docstring).
DEFAULT_PROMPT_TEMPLATE = """You are implementing a feature for this project.

## Feature Request
Title: {{title}}

Description:
{{description}}

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


def render_placeholders(template: str, title: str, description: str) -> str:
    """Substitute the frozen placeholder vocabulary into a template.

    Plain string replacement, exactly like the legacy renderer: no format
    spec, no expression evaluation - a prompt template is user content and
    must never be able to reach into the process.
    """
    rendered = template.replace("{{title}}", title or "")
    return rendered.replace("{{description}}", description or "")


def render_agent_prompt(
    *,
    card_title: str = "",
    card_description: str = "",
    prompt_template: Optional[str] = None,
    previous_step_logs: Optional[str] = None,
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

    Returns:
        The prompt text. Never None, never empty when a template exists.
    """
    template = prompt_template or DEFAULT_PROMPT_TEMPLATE
    prompt = render_placeholders(template, card_title, card_description)

    if previous_step_logs:
        prompt += PREVIOUS_STEP_SECTION.format(logs=previous_step_logs)

    return prompt

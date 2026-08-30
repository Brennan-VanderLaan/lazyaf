"""
Unit tests for the backend-side prompt renderer (Phase 12.5, design 2.5).

12.5 moved prompt rendering from the container to the backend, because the
backend already owns PromptTemplate, the card fields, the resolved AgentFiles
and (at 12.6.6) the spec bundle - a container that re-templates would be a
second source of truth for the most important string in the system.

12.6 DELETED the container-side renderer along with the polling entrypoint
that held it. The two parity tests that used to run both renderers in one
process and compare are now shape assertions against FROZEN literals copied
from that renderer's last behaviour. That is the honest replacement: the
contract those tests protected was never "the two agree", it was "the
placeholder vocabulary and the previous-step section keep this exact shape",
and the second renderer was only the most convenient way to state it. Deleting
them instead would have retired the contract along with the duplicate.

One deliberate DIVERGENCE is asserted rather than hidden: the control path
DROPS the README-scraping branch of the old default prompt. The agent can
read the repository itself, and 12.6.6 replaces that slot with curated spec
context.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (REPO_ROOT / "backend", REPO_ROOT / "runner-common"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.services.agent_prompt import (  # noqa: E402
    DEFAULT_PROMPT_TEMPLATE,
    PLACEHOLDERS,
    render_agent_prompt,
    render_placeholders,
)


class TestPlaceholderVocabulary:
    def test_the_vocabulary_is_exactly_title_and_description(self):
        assert set(PLACEHOLDERS) == {"{{title}}", "{{description}}"}

    def test_default_template_uses_every_placeholder(self):
        for placeholder in PLACEHOLDERS:
            assert placeholder in DEFAULT_PROMPT_TEMPLATE

    def test_substitution_matches_the_retired_container_renderer(self):
        """FROZEN from `runner_common.entrypoint.build_prompt`, which did
        exactly two `str.replace` calls and nothing else.

        That renderer was deleted in 12.6. The literal below is what it
        produced, so the surviving renderer is still held to the shape rather
        than to a counterpart that no longer exists to disagree with it.
        """
        template = "T={{title}} D={{description}}"
        assert render_placeholders(
            template, "Add pagination", "GET /items returns every row"
        ) == "T=Add pagination D=GET /items returns every row"

    def test_substitution_is_plain_string_replacement(self):
        """A prompt template is USER content: no format spec, no expression
        evaluation, nothing that could reach into the process."""
        rendered = render_placeholders(
            "{{title}} {value} {0} {{unknown}}", "T", "D"
        )
        assert rendered == "T {value} {0} {{unknown}}"

    def test_missing_values_render_empty_not_none(self):
        assert render_placeholders("[{{title}}][{{description}}]", "", "") == (
            "[][]"
        )


class TestDefaultTemplate:
    def test_default_used_when_no_template_given(self):
        prompt = render_agent_prompt(
            card_title="Add rate limiting", card_description="to /api/repos"
        )
        assert "You are implementing a feature for this project." in prompt
        assert "Title: Add rate limiting" in prompt
        assert "to /api/repos" in prompt

    def test_empty_template_falls_back_to_the_default(self):
        assert render_agent_prompt(
            card_title="X", card_description="Y", prompt_template=""
        ) == render_agent_prompt(card_title="X", card_description="Y")

    def test_step_template_wins_over_the_default(self):
        prompt = render_agent_prompt(
            card_title="X",
            card_description="Y",
            prompt_template="ONLY: {{title}}",
        )
        assert prompt == "ONLY: X"

    def test_readme_scraping_is_dropped_on_the_control_path(self):
        """Documented divergence from the legacy renderer, asserted so it
        cannot creep back in: the agent reads the repo itself."""
        assert "README" not in DEFAULT_PROMPT_TEMPLATE
        prompt = render_agent_prompt(card_title="X", card_description="Y")
        assert "Repository Context" not in prompt


class TestPreviousStepSection:
    def test_previous_logs_are_appended_in_the_legacy_section_shape(self):
        prompt = render_agent_prompt(
            card_title="X",
            card_description="Y",
            previous_step_logs="plan output here",
        )
        assert "## Previous Step Output" in prompt
        assert "plan output here" in prompt
        assert "Use this context when completing the current task." in prompt

    def test_section_matches_the_retired_container_renderer_byte_for_byte(self):
        """FROZEN from the deleted `build_prompt`, whitespace included.

        The blank line after the body, the fenced block, and the trailing
        newline are all part of what an agent actually reads, and every one of
        them was in the string that renderer appended.
        """
        expected = (
            "BODY\n"
            "\n"
            "## Previous Step Output\n"
            "The previous pipeline step produced the following output:\n"
            "```\n"
            "LOGS\n"
            "```\n"
            "\n"
            "Use this context when completing the current task.\n"
        )
        assert render_agent_prompt(
            card_title="t",
            card_description="d",
            prompt_template="BODY",
            previous_step_logs="LOGS",
        ) == expected

    def test_absent_logs_add_no_section(self):
        for value in (None, ""):
            prompt = render_agent_prompt(
                card_title="X", card_description="Y", previous_step_logs=value
            )
            assert "## Previous Step Output" not in prompt

    def test_logs_containing_braces_are_not_reinterpreted(self):
        """Log output is arbitrary bytes - `{}` in it must not blow up the
        .format() that builds the section."""
        prompt = render_agent_prompt(
            card_title="X",
            card_description="Y",
            previous_step_logs="dict is {'a': 1} and {logs}",
        )
        assert "dict is {'a': 1} and {logs}" in prompt


class TestRendererIsTotal:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"card_title": "X"},
            {"card_description": "Y"},
            {"prompt_template": "no placeholders at all"},
            {"card_title": "X", "previous_step_logs": "L"},
        ],
    )
    def test_always_returns_a_string(self, kwargs):
        prompt = render_agent_prompt(**kwargs)
        assert isinstance(prompt, str)
        assert prompt  # never empty: the default template always has a body

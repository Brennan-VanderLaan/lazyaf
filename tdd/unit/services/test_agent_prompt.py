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
    SPEC_CONTEXT_PLACEHOLDER,
    render_agent_prompt,
    render_placeholders,
)

#: A bundle the way `spec_context.build_spec_context` renders one: it carries
#: its OWN `## Spec Context` heading and ends with a newline.
BUNDLE = (
    "## Spec Context\n"
    "\n"
    "### Story: Operator sets a per-repo budget  (story abc12345)\n"
    "- [required] (criterion def67890) A repo over budget receives HTTP 429.\n"
)


class TestPlaceholderVocabulary:
    def test_the_vocabulary_is_title_description_and_spec_context(self):
        """12.6.6 added the third slot. `{{title}}`/`{{description}}` are still
        the pair the deleted container renderer had to agree on; the spec slot
        is control-path only, and double-braced for the same reason they are -
        one brace style per template, and no `str.format` anywhere near user
        content."""
        assert set(PLACEHOLDERS) == {
            "{{title}}",
            "{{description}}",
            "{{spec_context}}",
        }
        assert SPEC_CONTEXT_PLACEHOLDER == "{{spec_context}}"
        assert SPEC_CONTEXT_PLACEHOLDER in PLACEHOLDERS

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


# -----------------------------------------------------------------------------
# 12.6.6: the {{spec_context}} slot
#
# The lane shipped an assembler, a wire key and a container-side loader, all
# pinned from both sides, and NOTHING that put a bundle into the prompt. These
# tests pin the renderer half of the connection: where the bundle lands, and -
# the load-bearing half - that a card with no spec links produces the exact
# same bytes it did before 12.6.6 existed.
# -----------------------------------------------------------------------------

class TestSpecContextSlot:
    def test_the_default_template_reserves_the_slot(self):
        assert SPEC_CONTEXT_PLACEHOLDER in DEFAULT_PROMPT_TEMPLATE

    def test_the_bundle_lands_where_the_template_puts_it(self):
        prompt = render_agent_prompt(
            card_title="X",
            card_description="Y",
            prompt_template="BEFORE\n{{spec_context}}\nAFTER",
            spec_context="MIDDLE",
        )
        assert prompt == "BEFORE\nMIDDLE\nAFTER"

    def test_the_bundle_is_appended_when_the_template_has_no_slot(self):
        """Intent after the body, but BEFORE the previous-step section: the
        prompt's last instruction must stay 'Use this context when completing
        the current task.'"""
        prompt = render_agent_prompt(
            card_title="X",
            card_description="Y",
            prompt_template="BODY",
            spec_context=BUNDLE,
            previous_step_logs="LOGS",
        )
        assert prompt.index("BODY") < prompt.index("## Spec Context")
        assert prompt.index("## Spec Context") < prompt.index(
            "## Previous Step Output"
        )
        assert prompt.endswith(
            "Use this context when completing the current task.\n"
        )

    def test_appending_does_not_double_the_blank_line_before_the_log_section(
        self,
    ):
        """The frozen `## Previous Step Output` shape opens with its own blank
        line. Appending a bundle first must not grow a second one."""
        prompt = render_agent_prompt(
            prompt_template="BODY", spec_context="SPEC", previous_step_logs="L"
        )
        assert prompt == (
            "BODY\n"
            "\n"
            "SPEC\n"
            "\n"
            "## Previous Step Output\n"
            "The previous pipeline step produced the following output:\n"
            "```\n"
            "L\n"
            "```\n"
            "\n"
            "Use this context when completing the current task.\n"
        )

    @pytest.mark.parametrize("bundle", [None, ""])
    def test_no_bundle_renders_byte_identically_to_the_pre_12_6_6_prompt(
        self, bundle
    ):
        """THE no-op pin. `None` is the one spelling of 'this card has no spec
        context' - not `{}`, not an empty `## Spec Context` heading, and not a
        stray blank line where the slot used to be. The literal below is the
        default prompt as it rendered before the slot was added.
        """
        expected = (
            "You are implementing a feature for this project.\n"
            "\n"
            "## Feature Request\n"
            "Title: Add pagination\n"
            "\n"
            "Description:\n"
            "GET /items returns every row\n"
            "\n"
            "## Instructions\n"
            "1. Implement this feature following existing code patterns\n"
            "2. Write tests if a test framework is present\n"
            "3. Commit your changes with a clear message\n"
            "4. Do not modify unrelated code\n"
            "5. Keep changes minimal and focused\n"
        )
        assert (
            render_agent_prompt(
                card_title="Add pagination",
                card_description="GET /items returns every row",
                spec_context=bundle,
            )
            == expected
        )

    def test_no_bundle_collapses_the_slots_own_line_in_a_custom_template(self):
        assert (
            render_agent_prompt(prompt_template="A\n{{spec_context}}\nB")
            == "A\nB"
        )

    def test_no_bundle_never_leaves_an_empty_spec_heading(self):
        prompt = render_agent_prompt(card_title="X", card_description="Y")
        assert "Spec Context" not in prompt
        assert "{{spec_context}}" not in prompt

    def test_a_bundle_containing_the_placeholder_is_not_re_substituted(self):
        """Single-pass substitution IS the security property: nothing that
        comes out of a substitution goes back in."""
        prompt = render_agent_prompt(
            card_title="T",
            card_description="D",
            prompt_template="{{spec_context}}",
            spec_context="{{title}} and {{description}} and {{spec_context}}",
        )
        assert prompt == "{{title}} and {{description}} and {{spec_context}}"

    def test_a_bundle_containing_the_placeholder_does_not_retrigger_the_append(
        self,
    ):
        """Case 2-vs-3 is decided on the RAW template, before substitution."""
        prompt = render_agent_prompt(
            prompt_template="BODY",
            spec_context="SPEC {{spec_context}}",
        )
        assert prompt.count("SPEC") == 1

    def test_a_description_containing_the_placeholder_gets_no_bundle(self):
        """A card description is user content and must not be able to reach a
        second slot - one pass means the description is substituted, never
        re-scanned."""
        prompt = render_agent_prompt(
            card_title="T",
            card_description="{{spec_context}}",
            prompt_template="D={{description}}",
            spec_context="SECRETISH",
        )
        assert prompt == "D={{spec_context}}\n\nSECRETISH\n"

    def test_render_placeholders_substitutes_the_third_slot(self):
        assert (
            render_placeholders("[{{spec_context}}]", "T", "D", "S")
            == "[S]"
        )

    def test_render_placeholders_still_does_no_format_evaluation(self):
        assert render_placeholders(
            "{{title}} {value} {0} {{unknown}} {{spec_context}}", "T", "D", "S"
        ) == "T {value} {0} {{unknown}} S"

"""Configuration for the lead agent (system prompt template selection)."""

from pydantic import BaseModel, Field


class LeadAgentConfig(BaseModel):
    """Lead-agent runtime configuration.

    Currently only carries the system-prompt template override. Keeping it in a
    dedicated section leaves room for future lead-agent-only knobs without
    polluting the top-level ``AppConfig`` namespace.
    """

    system_prompt_path: str | None = Field(
        default=None,
        description=(
            "Path to a system-prompt template file for the default lead agent "
            "(relative to project root or absolute). When null, the built-in "
            "SYSTEM_PROMPT_TEMPLATE is used. Lets production swap prompt variants "
            "by editing config.yaml (picked up on the next message) instead of "
            "patching code. The file must only use placeholders that are a subset "
            "of apply_prompt_template's format kwargs."
        ),
    )

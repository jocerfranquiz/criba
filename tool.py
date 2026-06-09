"""Expose criba as an LLM / agent tool.

A framework-neutral tool definition — ``TOOL_NAME`` + ``TOOL_DESCRIPTION`` + a
standard JSON Schema (``TOOL_PARAMETERS``) — plus thin adapters for the common
function-calling shapes and a dispatcher that runs a call. The core library
(:mod:`criba`) stays free of any agent-integration glue.
"""

from __future__ import annotations

from criba import extract_text

__all__ = [
    "TOOL_NAME",
    "TOOL_DESCRIPTION",
    "TOOL_PARAMETERS",
    "as_openai_tool",
    "as_anthropic_tool",
    "call_tool",
]

TOOL_NAME = "criba_extract_text"
TOOL_DESCRIPTION = (
    "Extract a native-text PDF's content as structure-aware Markdown "
    "(headings, emphasis, inline image references) for RAG or analysis. "
    "Scanned PDFs without a text layer yield little or no text."
)
TOOL_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Filesystem path to the PDF."},
        "password": {
            "type": "string",
            "description": "Password, if the PDF is encrypted.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def as_openai_tool() -> dict:
    """The tool as an OpenAI function-calling spec."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "parameters": TOOL_PARAMETERS,
        },
    }


def as_anthropic_tool() -> dict:
    """The tool as an Anthropic tool-use spec."""
    return {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "input_schema": TOOL_PARAMETERS,
    }


def call_tool(arguments: dict) -> str:
    """Dispatch a tool call (the model's JSON arguments) to ``extract_text``.

    Returns the Markdown string to hand back as the tool result.
    """
    return extract_text(arguments["path"], password=arguments.get("password"))

"""Throwaway probe. Confirms the model provider answers and can emit a structured tool call."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Cheapest current stable model as of 2026-09-09 (confirmed against ai.google.dev/gemini-api/docs
# pricing and model docs at spike time): gemini-2.5-flash-lite. The design-time snippet used
# "gemini-2.0-flash", which current docs list as shut down.
MODEL = "gemini-2.5-flash-lite"

CLICK_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="click",
            description="Click a control identified by its observation index.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"index": types.Schema(type=types.Type.INTEGER)},
                required=["index"],
            ),
        )
    ]
)


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY")
    assert key, "GEMINI_API_KEY is not set in the environment"
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=MODEL,
        contents=(
            "Observation:\n"
            "  [0] textbox 'Member ID'\n"
            "  [1] button 'Search'\n"
            "Goal: run the search. Call exactly one tool."
        ),
        config=types.GenerateContentConfig(tools=[CLICK_TOOL]),
    )
    # response.function_calls is the SDK's own convenience accessor: it is None-safe against
    # missing candidates/content/parts, unlike manually walking response.candidates[i].content.parts.
    calls = response.function_calls
    print("tool calls:", calls)
    assert calls, "the provider returned no structured tool call"
    assert calls[0].args["index"] == 1


if __name__ == "__main__":
    main()

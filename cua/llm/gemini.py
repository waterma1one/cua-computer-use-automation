"""The one real `LLMClient` adapter (spec §8.2): Gemini, over its REST API directly through
`httpx` -- never the `google-genai` SDK (E1). D24 verified this exact path three times over
(list, generate, a responseSchema); the SDK's own request construction is the one thing D11's
spike never got to clear.

The credential travels only in the `x-goog-api-key` header, never in a URL, and no error
raised here interpolates it, the request URL or a response body.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from cua.llm.base import Completion, Message, ToolCall, ToolDef, Usage

__all__ = ["GeminiClient", "LLMError", "load_gemini_client_from_env"]

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_MODEL = "gemini-3.5-flash-lite"


class LLMError(RuntimeError):
    """A provider-level failure: bad credential, unsupported model, malformed response.
    Never raised for an ordinary model response, however unhelpful -- that is a
    `Completion`, handled by the discovery loop's own stopping conditions, not an error.
    """


def _upper_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's `Schema` vocabulary spells its `type` field in upper case
    (`"OBJECT"`/`"STRING"`/`"INTEGER"`); `cua.llm.base.ToolDef` is deliberately provider-
    neutral and lower-case (E2). This is the one place the translation happens, one level of
    `properties` deep -- every tool this phase declares (`cua.agent.tools.DISCOVERY_TOOLS`)
    is flat, so no deeper recursion is needed.
    """
    out = dict(schema)
    kind = out.get("type")
    if isinstance(kind, str):
        out["type"] = kind.upper()
    properties = out.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {name: _upper_schema(value) for name, value in properties.items()}
    return out


def _request_body(messages: list[Message], tools: list[ToolDef]) -> dict[str, object]:
    contents: list[dict[str, object]] = []
    system_text: str | None = None
    for message in messages:
        if message.role == "system":
            system_text = message.text
        elif message.role == "tool":
            contents.append({"role": "user", "parts": [{
                "functionResponse": {
                    "name": message.tool_name, "response": {"result": message.text},
                },
            }]})
        else:
            role = "model" if message.role == "model" else "user"
            contents.append({"role": role, "parts": [{"text": message.text}]})
    body: dict[str, object] = {
        "contents": contents,
        "tools": [{"functionDeclarations": [
            {"name": tool.name, "description": tool.description,
             "parameters": _upper_schema(tool.parameters)}
            for tool in tools
        ]}],
    }
    if system_text is not None:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    return body


def _parse_response(data: object) -> ToolCall | Completion:
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates or not isinstance(candidates, list) or not isinstance(candidates[0], dict):
        raise LLMError("no candidates in Gemini response")
    content = candidates[0].get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    for part in parts:
        function_call = part.get("functionCall") if isinstance(part, dict) else None
        if isinstance(function_call, dict) and isinstance(function_call.get("name"), str):
            return ToolCall(
                id=secrets.token_hex(4), name=function_call["name"],
                args=dict(function_call.get("args") or {}),
            )
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    return Completion(text=text)


def _parse_usage(data: object) -> Usage:
    """Token counts from the response's `usageMetadata`; zeros when absent or malformed."""
    meta = data.get("usageMetadata") if isinstance(data, dict) else None
    if not isinstance(meta, dict):
        return Usage()

    def count(key: str) -> int:
        value = meta.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    prompt, completion = count("promptTokenCount"), count("candidatesTokenCount")
    return Usage(prompt=prompt, completion=completion,
                 total=count("totalTokenCount") or prompt + completion)


def _models_supporting_generate_content(client: httpx.Client, api_key: str) -> list[str]:
    response = client.get(
        f"{_BASE_URL}/models", headers={"x-goog-api-key": api_key}, params={"pageSize": 200},
    )
    response.raise_for_status()
    supported = []
    for model in response.json().get("models", []):
        methods = model.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            supported.append(str(model.get("name", "")).removeprefix("models/"))
    return supported


@dataclass
class GeminiClient:
    """`LLMClient` (structurally). `api_key`/`model` are both configuration (D24) -- never a
    constant in code. `api_key` is excluded from `repr` so it cannot leak into a log line.
    """

    api_key: str = field(repr=False)
    model: str
    _client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30.0), repr=False)
    usage: Usage = field(default_factory=Usage)

    def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
        try:
            response = self._client.post(
                f"{_BASE_URL}/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=_request_body(messages, tools),
            )
            if response.status_code == 404:
                supported = _models_supporting_generate_content(self._client, self.api_key)
                raise LLMError(
                    f"model {self.model!r} does not support generateContent (404); models "
                    f"that currently do: {supported}"
                )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"Gemini request failed with HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"Gemini request failed: {type(exc).__name__}") from None
        except ValueError:
            raise LLMError("Gemini returned a non-JSON response") from None
        self.usage.add(_parse_usage(data))
        return _parse_response(data)


def load_gemini_client_from_env() -> GeminiClient:
    """Reads `GEMINI_API_KEY` (required) and `CUA_LLM_MODEL` (default `gemini-3.5-flash-
    lite`, D24) from the process environment. Does not itself call `load_dotenv()` -- that is
    a side effect `cua/cli.py` owns once, at the module level (E12), not something a library
    function should trigger as a side effect of being imported or called.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMError("GEMINI_API_KEY must be set (see .env.example)")
    model = os.environ.get("CUA_LLM_MODEL") or _DEFAULT_MODEL
    return GeminiClient(api_key=api_key, model=model)

"""GeminiClient, tested against httpx.MockTransport -- no test here reaches a real network,
and none needs a real key (acceptance criterion 1 holds for this adapter too, even though
it is the one module in this phase that speaks HTTP at all).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cua.agent.tools import DISCOVERY_TOOLS
from cua.llm.base import Completion, LLMClient, Message, ToolCall
from cua.llm.gemini import GeminiClient, LLMError, load_gemini_client_from_env

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, model: str = "gemini-3.5-flash-lite") -> GeminiClient:
    client = GeminiClient(api_key="test-key", model=model)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_gemini_client_satisfies_the_protocol() -> None:
    assert isinstance(GeminiClient(api_key="k", model="m"), LLMClient)


def test_step_sends_the_api_key_as_a_header_and_uppercases_schema_types() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["query"] = request.url.query
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "click", "args": {"index": 3}}}
        ]}}]})

    result = _client(handler).step([Message(role="user", text="go")], DISCOVERY_TOOLS)

    assert seen["headers"]["x-goog-api-key"] == "test-key"
    assert b"test-key" not in seen["query"]
    fn_decls = seen["json"]["tools"][0]["functionDeclarations"]
    click_decl = next(d for d in fn_decls if d["name"] == "click")
    assert click_decl["parameters"]["type"] == "OBJECT"
    assert click_decl["parameters"]["properties"]["index"]["type"] == "INTEGER"
    assert isinstance(result, ToolCall)
    assert result.name == "click"
    assert result.args == {"index": 3}


def test_step_maps_system_and_tool_messages() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "x"}]}}]})

    _client(handler).step(
        [
            Message(role="system", text="be brief"),
            Message(role="user", text="go"),
            Message(role="tool", text="done", tool_name="click", tool_call_id="1"),
        ],
        DISCOVERY_TOOLS,
    )
    assert seen["json"]["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    tool_turn = seen["json"]["contents"][1]
    assert tool_turn["parts"][0]["functionResponse"]["name"] == "click"


def test_step_returns_a_completion_when_the_model_returns_plain_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "I am not sure what to do."}
        ]}}]})

    result = _client(handler).step([Message(role="user", text="go")], DISCOVERY_TOOLS)
    assert isinstance(result, Completion)
    assert result.text == "I am not sure what to do."


def test_a_404_reports_the_configured_id_and_the_ids_that_still_support_generatecontent() -> None:
    # D24's own requirement: a bare 404 sends the reader to the wrong question.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":generateContent"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.5-flash-lite",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-lite",
             "supportedGenerationMethods": ["countTokens"]},
        ]})

    client = _client(handler, model="gemini-9000-nonexistent")
    with pytest.raises(LLMError) as exc_info:
        client.step([Message(role="user", text="go")], DISCOVERY_TOOLS)
    message = str(exc_info.value)
    assert "gemini-9000-nonexistent" in message
    assert "gemini-3.5-flash-lite" in message
    assert "gemini-2.5-flash-lite" not in message  # it does not support generateContent


def test_an_http_error_becomes_an_llm_error_without_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    with pytest.raises(LLMError) as exc_info:
        _client(handler).step([Message(role="user", text="go")], DISCOVERY_TOOLS)
    assert "403" in str(exc_info.value)
    assert "test-key" not in str(exc_info.value)


def test_a_transport_failure_becomes_an_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(LLMError, match="ConnectError"):
        _client(handler).step([Message(role="user", text="go")], DISCOVERY_TOOLS)


def test_a_response_with_no_candidates_is_an_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(LLMError, match="no candidates"):
        _client(handler).step([Message(role="user", text="go")], DISCOVERY_TOOLS)


def test_the_key_is_not_in_the_repr() -> None:
    assert "secret-value" not in repr(GeminiClient(api_key="secret-value", model="m"))


def test_load_from_env_reads_the_key_and_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.setenv("CUA_LLM_MODEL", "gemini-9.9-preview")
    client = load_gemini_client_from_env()
    assert client.api_key == "env-key"
    assert client.model == "gemini-9.9-preview"


def test_load_from_env_defaults_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.delenv("CUA_LLM_MODEL", raising=False)
    assert load_gemini_client_from_env().model == "gemini-3.5-flash-lite"


def test_load_from_env_refuses_with_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        load_gemini_client_from_env()

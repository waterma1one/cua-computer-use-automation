"""FakeClient is every other task's LLM double from here on -- acceptance criterion 1 (every
test in this phase uses FakeClient; no test requires a key or a network) starts here.
"""
import pytest

from cua.llm.base import Completion, LLMClient, Message, ToolCall, ToolDef
from cua.llm.fake import FakeClient


def test_fake_client_satisfies_the_protocol() -> None:
    assert isinstance(FakeClient(script=[]), LLMClient)


def test_fake_client_returns_its_script_in_order() -> None:
    call = ToolCall(id="1", name="click", args={"index": 0})
    completion = Completion(text="done")
    client = FakeClient(script=[call, completion])
    tools = [ToolDef(name="click", description="", parameters={"type": "object"})]
    messages = [Message(role="user", text="go")]

    assert client.step(messages, tools) is call
    assert client.step(messages, tools) is completion


def test_fake_client_records_every_call() -> None:
    client = FakeClient(script=[Completion(text="done")])
    messages = [Message(role="system", text="s"), Message(role="user", text="u")]
    tools = [ToolDef(name="finish", description="", parameters={"type": "object"})]
    client.step(messages, tools)
    assert client.calls == [(messages, tools)]


def test_fake_client_raises_when_the_script_is_exhausted() -> None:
    client = FakeClient(script=[])
    with pytest.raises(AssertionError, match="script exhausted"):
        client.step([], [])

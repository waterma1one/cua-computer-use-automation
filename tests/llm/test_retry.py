import pytest

from cua.llm.base import Completion, LLMClient, Message, ToolCall, ToolDef
from cua.llm.gemini import LLMError
from cua.llm.retry import RetryingClient

CALL = ToolCall(id="1", name="click", args={"index": 0})


class _Flaky:
    model = "m"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall | Completion:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise LLMError("boom")
        return CALL


def test_retrying_client_satisfies_the_protocol() -> None:
    assert isinstance(RetryingClient(_Flaky(0)), LLMClient)


def test_retries_then_succeeds_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    inner = _Flaky(2)
    client = RetryingClient(inner, attempts=3, base_delay_s=0.5, sleep=sleeps.append)
    assert client.step([], []) is CALL
    assert inner.attempts == 3
    assert sleeps == [0.5, 1.0]


def test_raises_the_last_error_once_attempts_are_exhausted() -> None:
    sleeps: list[float] = []
    inner = _Flaky(10)
    client = RetryingClient(inner, attempts=3, base_delay_s=1.0, sleep=sleeps.append)
    with pytest.raises(LLMError, match="boom"):
        client.step([], [])
    assert inner.attempts == 3
    assert sleeps == [1.0, 2.0]


def test_does_not_retry_a_non_llm_error() -> None:
    class _Broken:
        def step(self, messages: list[Message], tools: list[ToolDef]) -> ToolCall:
            raise AssertionError("script exhausted")

    with pytest.raises(AssertionError):
        RetryingClient(_Broken(), sleep=lambda _s: None).step([], [])


def test_model_is_taken_from_the_wrapped_client() -> None:
    assert RetryingClient(_Flaky(0)).model == "m"
    assert RetryingClient(object()).model == "unknown"  # type: ignore[arg-type]

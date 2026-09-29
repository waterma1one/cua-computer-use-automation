"""What the model sees each turn: the goal once, prior tool traffic, then the current
observation rendered as an indexed list."""
from __future__ import annotations

from cua.agent.prompt import build_messages, observation_message
from cua.llm.base import Message
from cua.surface.models import Node, NodeState, Observation
from tests.agent.conftest import PATH, node


def test_build_messages_orders_system_history_then_current_observation() -> None:
    obs = Observation(generation=1, nodes=[node("textbox", "Member ID")], truncated=False)
    history = [Message(role="model", text="called expand({})"),
               Message(role="tool", text="expanded", tool_name="expand", tool_call_id="1")]
    messages = build_messages("Read the balance.", obs, history)
    assert [m.role for m in messages] == ["system", "model", "tool", "user"]
    assert "Goal: Read the balance." in messages[0].text
    assert messages[1:3] == history
    assert messages[-1] == observation_message(obs)


def test_build_messages_does_not_mutate_history() -> None:
    history: list[Message] = []
    obs = Observation(generation=1, nodes=[], truncated=False)
    build_messages("g", obs, history)
    assert history == []


def test_observation_rendering_shows_index_role_name_value_state_and_truncation() -> None:
    disabled = Node(index=2, role="button", name="Post", state=NodeState(disabled=True),
                    surface_path=PATH)
    obs = Observation(generation=4, truncated=True, nodes=[
        node("textbox", "Member ID", index=0),
        node("cell", "Savings", value="1234.56", index=1),
        disabled,
        node("generic", index=3),
    ])
    text = observation_message(obs).text
    assert "truncated -- call expand" in text
    assert '0: textbox "Member ID"' in text
    assert "1: cell \"Savings\" = '1234.56'" in text
    assert '2: button "Post" [disabled]' in text
    assert "\n3: generic" in text

"""Spec §7.5: human-action capture is best-effort and cannot be the sole audit record --
the window is bracketed with before/after screenshots and snapshots, which IS fully
verifiable, and this test suite proves exactly that half, per the plan's own Risks note (do
not write a test asserting the continuous capture caught everything).
"""
from cua.session.actions import HumanAction, HumanActionBracket, record_human_action


class RecordingSink:
    def __init__(self) -> None:
        self.run_id = "run-test"
        self.events: list[dict] = []
        self.frames: list[tuple[object, str]] = []

    def event(self, **fields: object) -> None:
        self.events.append(dict(fields))

    def frame(self, frame: object, name: str) -> None:
        self.frames.append((frame, name))

    def evidence_ref(self) -> str:
        return f"evidence/{self.run_id}"


def test_record_human_action_always_stamps_human_origin_true() -> None:
    sink = RecordingSink()
    action = record_human_action(
        sink, "op-1",
        {"type": "click", "role": "button", "name": "Search", "value": None,
         "url": "http://h/search", "timestamp": 1234},
    )
    assert action.human_origin is True
    assert action.operator_id == "op-1"
    assert sink.events == [{
        "kind": "human_action", "operator_id": "op-1", "human_origin": True,
        "type": "click", "role": "button", "name": "Search", "value": None,
        "url": "http://h/search", "timestamp": 1234,
    }]


def test_human_action_requires_human_origin_with_no_default() -> None:
    # E20: a defaulted field would still be an ordinary settable kwarg regardless of its
    # Literal[True] annotation (checked directly: mypy would flag human_origin=False as a
    # type error, but nothing stops it from running). Dropping the default at least forces
    # every construction site to say so explicitly; the REAL guarantee -- that
    # record_human_action is the only production call site that does -- is checked
    # structurally in tests/test_architecture.py, not here.
    import inspect

    params = inspect.signature(HumanAction).parameters
    assert "human_origin" in params
    assert params["human_origin"].default is inspect.Parameter.empty


class FakeSurface:
    def __init__(self) -> None:
        self.captures = 0

    def capture(self):
        self.captures += 1
        return object()

    def pending_dialog(self) -> str | None:
        return None


def test_the_bracket_captures_a_frame_before_and_after() -> None:
    sink = RecordingSink()
    surface = FakeSurface()
    bracket = HumanActionBracket()
    bracket.before(surface, sink)
    bracket.after(surface, sink)
    assert surface.captures == 2
    assert [name for _frame, name in sink.frames] == ["human_before", "human_after"]


def test_a_pending_dialog_skips_the_before_capture_without_raising() -> None:
    class DialogSurface(FakeSurface):
        def pending_dialog(self) -> str | None:
            return "Are you sure?"

    sink = RecordingSink()
    surface = DialogSurface()
    HumanActionBracket().before(surface, sink)
    assert surface.captures == 0
    assert sink.events == [{"kind": "human_bracket_skipped", "bracket": "before",
                            "reason": "a native dialog is pending"}]

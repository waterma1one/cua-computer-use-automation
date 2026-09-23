"""Spec §7.1: the lease is an agent-side mutex and an audit record of intent. Every act on a
surface requires the token; an actor without it is rejected in code -- proven directly here,
not assumed by anything downstream.
"""
import pytest

from cua.session.lease import Lease, LeasedSurface, LeaseError
from cua.surface.base import Surface, SurfaceError
from cua.surface.models import Action, ActionResult, EvidenceFrame, Locator, Observation, Resolution


class FakeSurface:
    """Records every call it received, for the tests below to assert against."""

    def __init__(self) -> None:
        self.acts: list[Action] = []
        self.act_on_indexes: list[tuple[int, int, str]] = []
        self.observes = 0
        self.violation: str | None = None

    def observe(self) -> Observation:
        self.observes += 1
        return Observation(generation=1, nodes=[], truncated=False)

    def act(self, action: Action) -> ActionResult:
        self.acts.append(action)
        return ActionResult(ok=True, action=action, read_value=None)

    def resolve(self, locator: Locator) -> Resolution:
        raise NotImplementedError

    def capture(self) -> EvidenceFrame:
        return EvidenceFrame(generation=1, image_png=None, snapshot_yaml="")

    def pending_dialog(self) -> str | None:
        return None

    def act_on_index(self, generation: int, index: int, action: str) -> ActionResult:
        self.act_on_indexes.append((generation, index, action))
        return ActionResult(ok=True, action=Action(kind="click", locator=None), read_value=None)

    def allowlist_violation(self) -> str | None:
        return self.violation


assert isinstance(FakeSurface(), Surface)  # module import time: conformance, not just shape


def test_a_fresh_lease_has_no_controller_and_no_token() -> None:
    lease = Lease()
    assert lease.controller == "none"
    assert lease.token is None
    assert lease.holds("anything") is False


def test_acquire_mints_a_token_and_becomes_the_holder() -> None:
    lease = Lease()
    token = lease.acquire("agent")
    assert lease.controller == "agent"
    assert lease.token == token
    assert lease.holds(token) is True
    assert lease.holds("some-other-token") is False


def test_acquire_refuses_when_already_held() -> None:
    lease = Lease()
    lease.acquire("agent")
    with pytest.raises(ValueError, match="already held"):
        lease.acquire("operator")


def test_release_clears_the_controller_and_the_token() -> None:
    lease = Lease()
    token = lease.acquire("agent")
    lease.release()
    assert lease.controller == "none"
    assert lease.token is None
    assert lease.holds(token) is False


def test_transfer_moves_control_and_mints_a_new_token_without_an_intermediate_release() -> None:
    lease = Lease()
    old_token = lease.acquire("agent")
    new_token = lease.transfer("operator")
    assert new_token != old_token
    assert lease.controller == "operator"
    assert lease.holds(old_token) is False
    assert lease.holds(new_token) is True


def test_transfer_from_none_is_refused() -> None:
    lease = Lease()
    with pytest.raises(ValueError, match="not held"):
        lease.transfer("operator")


# --- LeasedSurface: acceptance criterion 1, proven directly ---------------------------------

def test_act_without_the_current_token_is_rejected() -> None:
    lease = Lease()
    lease.acquire("agent")
    fake = FakeSurface()
    surface = LeasedSurface(fake, lease, "wrong-token")
    with pytest.raises(LeaseError, match="does not hold the lease"):
        surface.act(Action(kind="click", locator=None))
    assert fake.acts == []  # never reached the underlying surface


def test_act_with_the_current_token_reaches_the_underlying_surface() -> None:
    lease = Lease()
    token = lease.acquire("agent")
    fake = FakeSurface()
    surface = LeasedSurface(fake, lease, token)
    action = Action(kind="click", locator=None)
    result = surface.act(action)
    assert result.ok is True
    assert fake.acts == [action]


def test_a_token_that_was_valid_is_rejected_after_the_lease_transfers_away() -> None:
    lease = Lease()
    agent_token = lease.acquire("agent")
    fake = FakeSurface()
    surface = LeasedSurface(fake, lease, agent_token)
    assert surface.act(Action(kind="click", locator=None)).ok is True
    lease.transfer("operator")
    with pytest.raises(LeaseError):
        surface.act(Action(kind="click", locator=None))
    assert len(fake.acts) == 1  # the second call never reached the surface


def test_act_on_index_is_gated_the_same_way() -> None:
    lease = Lease()
    lease.acquire("agent")
    fake = FakeSurface()
    surface = LeasedSurface(fake, lease, "wrong-token")
    with pytest.raises(LeaseError):
        surface.act_on_index(1, 0, "click")
    assert fake.act_on_indexes == []


def test_perception_is_never_gated_by_the_lease() -> None:
    # Spec §7.1: the lease governs acting, not observing -- a human's own eyes on the
    # headed browser need no token, and the automation must be able to see state to build
    # its next action even while it does not (yet, or any longer) hold the lease.
    lease = Lease()
    fake = FakeSurface()
    surface = LeasedSurface(fake, lease, "never-acquired")
    surface.observe()
    surface.capture()
    surface.pending_dialog()
    surface.allowlist_violation()
    assert fake.observes == 1  # reached the underlying surface despite no lease held


def test_leased_surface_satisfies_the_surface_protocol() -> None:
    lease = Lease()
    lease.acquire("agent")
    surface = LeasedSurface(FakeSurface(), lease, lease.token or "")
    assert isinstance(surface, Surface)


def test_lease_error_is_a_surface_error() -> None:
    # So a caller that only knows how to catch SurfaceError still catches this -- D40's
    # existing SurfaceError translation paths must not need a special case for it.
    assert issubclass(LeaseError, SurfaceError)


# --- rebind: E18, acceptance criterion 7's real fix ------------------------------------------

def test_rebind_lets_the_same_wrapper_accept_a_new_token_after_a_transfer() -> None:
    # This is the mechanism a session-owning replay thread's own LeasedSurface (Task 6) uses
    # to keep working after the lease transfers away to an operator and back to the agent --
    # the SAME object identity, not a reconstructed one, since nothing outside this test
    # holds a reference to swap a reconstructed object in for.
    lease = Lease()
    agent_token = lease.acquire("agent")
    fake = FakeSurface()
    surface = LeasedSurface(fake, lease, agent_token)
    assert surface.act(Action(kind="click", locator=None)).ok is True

    lease.transfer("operator")
    with pytest.raises(LeaseError):
        surface.act(Action(kind="click", locator=None))  # stale token: correctly rejected

    new_agent_token = lease.transfer("agent")
    surface.rebind(new_agent_token)
    result = surface.act(Action(kind="click", locator=None))
    assert result.ok is True
    assert len(fake.acts) == 2  # the first and this one -- the rejected middle call never landed

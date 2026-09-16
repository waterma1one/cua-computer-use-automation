"""Spec §6.1: deny first and wins; a policy narrows, never widens; the allowlist gates the
action type while risk gates the step. Pure -- no browser.
"""
import pytest

from cua.artifact.models import CapabilityPolicy
from cua.artifact.validate import DeploymentAllowlist
from cua.policy.allowlist import Decision, check_action, check_navigation, navigation_guard

ORIGIN = "http://127.0.0.1:8000"
DEPLOYMENT = DeploymentAllowlist(
    allowed_origins=[ORIGIN],
    allowed_paths=["/"],
    denied_paths=["/account/close"],
    allowed_actions=["navigate", "click", "fill", "read"],
)


# --- criterion 1 ---------------------------------------------------------------------------

def test_a_denied_path_inside_an_allowed_origin_and_prefix_is_still_refused() -> None:
    # /account/close sits inside the allowed origin and inside the allowed prefix "/".
    # Allow-only rules cannot express this; deny precedence is a requirement (§6.1).
    decision = check_navigation(f"{ORIGIN}/account/close?number=000100045512-01", DEPLOYMENT)
    assert decision == Decision(
        allowed=False, kind="ALLOWLIST_VIOLATION",
        reason=("path '/account/close' is denied by prefix '/account/close' "
                "(deny rules are evaluated first and win)"),
    )
    assert DEPLOYMENT.permits_url(f"{ORIGIN}/account/close?number=1") is False
    assert DEPLOYMENT.permits_url(f"{ORIGIN}/account/000100045512-01") is True


def test_an_unlisted_origin_is_refused_before_the_path_is_looked_at() -> None:
    decision = check_navigation("http://evil.example/", DEPLOYMENT)
    assert not decision.allowed and decision.kind == "ALLOWLIST_VIOLATION"
    assert decision.reason == "origin 'http://evil.example' is not an allowed origin"


def test_a_path_outside_every_allowed_prefix_is_refused() -> None:
    narrow = DEPLOYMENT.model_copy(update={"allowed_paths": ["/teller/"]})
    decision = check_navigation(f"{ORIGIN}/admin", narrow)
    assert not decision.allowed
    assert decision.reason == "path '/admin' is outside every allowed prefix ['/teller/']"


def test_only_http_and_https_urls_can_be_permitted() -> None:
    assert DEPLOYMENT.permits_url("about:blank") is False
    assert DEPLOYMENT.permits_url("javascript:void(0)") is False
    assert DEPLOYMENT.permits_url("chrome-error://chromewebdata/") is False


def test_origin_matching_ignores_case_and_a_trailing_slash() -> None:
    allow = DeploymentAllowlist(allowed_origins=["HTTP://Localhost:8000/"], allowed_paths=["/"])
    assert allow.permits_origin("http://localhost:8000") is True
    assert allow.permits_url("http://localhost:8000/x") is True
    assert allow.permits_origin("http://localhost:8001") is False


def test_a_permitted_navigation_is_a_decision_with_no_kind() -> None:
    assert check_navigation(f"{ORIGIN}/member/12345", DEPLOYMENT) == Decision(
        allowed=True, kind=None, reason="")


def test_the_guard_returns_none_when_permitted_and_the_reason_when_not() -> None:
    guard = navigation_guard(DEPLOYMENT)
    assert guard(f"{ORIGIN}/member/12345") is None
    assert guard(f"{ORIGIN}/account/close?number=1") == check_navigation(
        f"{ORIGIN}/account/close?number=1", DEPLOYMENT).reason


@pytest.mark.parametrize("url", [
    f"{ORIGIN}//account/close?number=1",
    f"{ORIGIN}/%2Faccount/close",
    f"{ORIGIN}/x/../account/close",
    f"{ORIGIN}/account/./close/",
])
def test_a_denied_path_cannot_be_reached_through_unnormalised_syntax(url: str) -> None:
    # Reviewer probe: `//account/close` and `/%2Faccount/close` sailed past a raw
    # `startswith`. Deny rules are evaluated on the path a routing layer would see.
    assert DEPLOYMENT.permits_url(url) is False
    decision = check_navigation(url, DEPLOYMENT)
    assert not decision.allowed
    assert "denied by prefix '/account/close'" in decision.reason


def test_permits_path_normalises_the_static_target_too() -> None:
    assert DEPLOYMENT.permits_path("//account/close?number=1") is False
    narrow = DEPLOYMENT.model_copy(update={"allowed_paths": ["/teller/"]})
    assert narrow.permits_path("/teller/") is True
    assert narrow.permits_path("/teller") is False


@pytest.mark.parametrize("path", [
    "/account/close?x=/../../foo",
    "/account/close#/../..",
    "/account/close%3Fx=/../../foo",
    "/account/close%23/../..",
])
def test_a_query_or_fragment_cannot_walk_a_static_target_out_of_a_deny_prefix(
    path: str,
) -> None:
    # `cua.replay.engine` hands `Step.target.path` to `permits_path` whole. Resolving the
    # whole string left `/account/close?x=/../../foo` as `/foo` -- outside the deny prefix
    # and inside the allowed `/`. The query and the fragment are removed after decoding,
    # so an encoded separator cannot reappear once the split has already happened.
    assert DEPLOYMENT.permits_path(path) is False
    assert DEPLOYMENT.denying_prefix(path) == "/account/close"


def test_a_legitimate_query_string_is_not_treated_as_a_bypass() -> None:
    # The mock application's fault switch is a query parameter; dropping the query must
    # not drop the path it belongs to.
    assert DEPLOYMENT.permits_path("/member/12345?fault=not_found") is True
    assert DEPLOYMENT.permits_path("/member/12345#balance") is True


def test_credentials_in_a_url_never_reach_the_reason() -> None:
    decision = check_navigation("http://user:pw@evil.example/", DEPLOYMENT)
    assert not decision.allowed
    assert "pw" not in decision.reason and "user:" not in decision.reason
    assert decision.reason == "origin 'http://evil.example' is not an allowed origin"
    # And credentials cannot smuggle a permitted host past the origin check either.
    host = ORIGIN.removeprefix("http://")
    assert DEPLOYMENT.permits_url(f"http://user:pw@{host}/member/1") is True


# --- criterion 4: narrowing -----------------------------------------------------------------

def test_a_policy_narrows_paths_and_actions_by_intersection() -> None:
    policy = CapabilityPolicy(allowed_paths=["/member/"], allowed_actions=["read", "click"])
    effective = DEPLOYMENT.narrowed_by(policy)
    assert effective.allowed_paths == ["/member/"]
    assert effective.allowed_actions == ["read", "click"]
    assert effective.denied_paths == ["/account/close"]
    assert effective.allowed_origins == [ORIGIN]


def test_a_policy_cannot_widen_by_construction() -> None:
    # Even without the validator's POLICY_WIDENS_ALLOWLIST having run, intersection drops
    # whatever the deployment does not permit -- including a re-permitted denied path.
    policy = CapabilityPolicy(
        allowed_paths=["/member/", "/account/close", "/elsewhere/"],
        allowed_actions=["read", "select"],
    )
    narrow = DEPLOYMENT.model_copy(update={"allowed_paths": ["/member/", "/account/"]})
    effective = narrow.narrowed_by(policy)
    assert effective.allowed_paths == ["/member/"]
    assert effective.allowed_actions == ["read"]
    for url in (f"{ORIGIN}/member/1", f"{ORIGIN}/account/close", f"{ORIGIN}/elsewhere/"):
        assert not (effective.permits_url(url) and not narrow.permits_url(url))


def test_a_none_field_means_not_narrowed_and_no_policy_means_the_deployment_itself() -> None:
    assert DEPLOYMENT.narrowed_by(None) is DEPLOYMENT
    effective = DEPLOYMENT.narrowed_by(CapabilityPolicy(allowed_paths=None, allowed_actions=None))
    assert effective.allowed_paths == DEPLOYMENT.allowed_paths
    assert effective.allowed_actions == DEPLOYMENT.allowed_actions


# --- check_action: the action type is the allowlist's; the step is risk's -------------------

def test_an_action_type_the_deployment_does_not_list_is_an_allowlist_violation() -> None:
    decision = check_action("select", "safe", "approved", DEPLOYMENT)
    assert decision == Decision(
        allowed=False, kind="ALLOWLIST_VIOLATION",
        reason="action 'select' is not permitted by the deployment allowlist "
               "(permitted: ['navigate', 'click', 'fill', 'read'])",
    )


def test_an_unclassified_step_is_policy_blocked() -> None:
    decision = check_action("click", None, "approved", DEPLOYMENT)
    assert not decision.allowed and decision.kind == "POLICY_BLOCKED"
    assert decision.reason == "the step has no risk classification"


@pytest.mark.parametrize("risk", ["risky", "irreversible"])
def test_a_risky_or_irreversible_step_requires_an_approved_artifact(risk: str) -> None:
    blocked = check_action("click", risk, "draft", DEPLOYMENT)  # type: ignore[arg-type]
    assert not blocked.allowed and blocked.kind == "POLICY_BLOCKED"
    article = "an" if risk == "irreversible" else "a"
    assert (blocked.reason
            == f"{article} {risk} step requires status 'approved'; the artifact is 'draft'")
    assert check_action("click", risk, "approved", DEPLOYMENT).allowed  # type: ignore[arg-type]


def test_a_safe_step_runs_from_a_draft() -> None:
    assert check_action("read", "safe", "draft", DEPLOYMENT) == Decision(True, None, "")


def test_with_no_allowlist_only_the_risk_and_status_legs_run() -> None:
    assert check_action("select", "safe", "draft", None).allowed
    assert not check_action("select", "risky", "draft", None).allowed

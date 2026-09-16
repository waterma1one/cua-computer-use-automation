"""Spec §6.1's decisions, explained: `check_navigation` for a live URL, `check_action` for
a step's action type against the deployment and its risk against the registry status.

The verdicts come from `DeploymentAllowlist` itself (`permits_url`, `permits_path`,
`allowed_actions`) -- D28: one security-relevant rule, one implementation. This module
only decides *which failure kind* a refusal is and composes the sentence that explains it:
an allowlist refusal is `ALLOWLIST_VIOLATION` (the deployment does not permit the *type* of
thing), a risk/status refusal is `POLICY_BLOCKED` (this *step* may not run unattended).
`confirm_irreversible` and the idempotency key are call-level arguments and stay in the
replay engine's own gate (E15).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from cua.artifact.models import FailureKind, RegistryStatus, Risk
from cua.artifact.validate import DeploymentAllowlist, origin_of
from cua.surface.base import NavigationGuard
from cua.surface.models import ActionKind

__all__ = ["Decision", "check_action", "check_navigation", "navigation_guard"]


@dataclass(frozen=True)
class Decision:
    """`allowed=True` carries no kind and an empty reason; a refusal names the
    `FailureKind` the replay engine returns for it and a sentence a human can act on."""

    allowed: bool
    kind: FailureKind | None
    reason: str


_PERMITTED = Decision(allowed=True, kind=None, reason="")


def check_navigation(url: str, allowlist: DeploymentAllowlist) -> Decision:
    """Whether a live, absolute URL may be visited. The verdict is `permits_url`'s; the
    legs are re-walked here only to say which one refused."""
    if allowlist.permits_url(url):
        return _PERMITTED
    parts = urlsplit(url)
    origin = origin_of(url) or url
    if parts.scheme not in ("http", "https") or not allowlist.permits_origin(origin):
        reason = f"origin {origin!r} is not an allowed origin"
    else:
        path = parts.path or "/"
        denied = next((p for p in allowlist.denied_paths if path.startswith(p)), None)
        if denied is not None:
            reason = (f"path {path!r} is denied by prefix {denied!r} "
                      "(deny rules are evaluated first and win)")
        else:
            reason = f"path {path!r} is outside every allowed prefix {allowlist.allowed_paths!r}"
    return Decision(allowed=False, kind="ALLOWLIST_VIOLATION", reason=reason)


def check_action(
    action: ActionKind, risk: Risk | None, status: RegistryStatus,
    allowlist: DeploymentAllowlist | None,
) -> Decision:
    """Whether one step may run unattended in this deployment, at this registry status.
    The action-type leg is skipped when no allowlist is supplied; the risk legs always run
    (§6.2's replay column: `risky` and `irreversible` require `approved`)."""
    if allowlist is not None and action not in allowlist.allowed_actions:
        return Decision(
            allowed=False, kind="ALLOWLIST_VIOLATION",
            reason=(f"action {action!r} is not permitted by the deployment allowlist "
                    f"(permitted: {allowlist.allowed_actions!r})"),
        )
    if risk is None:
        return Decision(allowed=False, kind="POLICY_BLOCKED",
                        reason="the step has no risk classification")
    if risk != "safe" and status != "approved":
        return Decision(
            allowed=False, kind="POLICY_BLOCKED",
            reason=f"a {risk} step requires status 'approved'; the artifact is {status!r}",
        )
    return _PERMITTED


def navigation_guard(allowlist: DeploymentAllowlist) -> NavigationGuard:
    """The callable `cua.surface.web.WebSurface` takes (E4): `None` when `url` is permitted,
    otherwise the reason. The surface never imports this package; the CLI builds the guard
    and hands it over."""
    def guard(url: str) -> str | None:
        decision = check_navigation(url, allowlist)
        return None if decision.allowed else decision.reason
    return guard

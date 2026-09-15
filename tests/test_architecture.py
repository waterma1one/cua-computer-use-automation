from pathlib import Path

import pytest

FORBIDDEN = ("playwright", "selenium")
PURE_PACKAGES = ("artifact", "replay", "policy", "catalog", "observability")


def test_only_surface_may_import_a_browser_driver() -> None:
    for package in PURE_PACKAGES:
        root = Path("cua") / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text().lower()
            for name in FORBIDDEN:
                assert f"import {name}" not in text, f"{path} imports {name}"
                assert f"from {name}" not in text, f"{path} imports {name}"


def test_the_boundary_check_can_actually_detect_an_import() -> None:
    # Without this, the test above passes vacuously until the pure packages exist, and a test
    # that cannot fail is not a test. Once phase 2 lands, this asserts the detector works by
    # confirming it sees the one import that is supposed to be there.
    web = Path("cua/surface/web.py")
    if not web.exists():
        pytest.skip("phase 2 has not landed yet")
    assert "playwright" in web.read_text().lower()


def test_the_protected_name_rule_has_exactly_one_implementation() -> None:
    """E6: one security-relevant rule, one implementation, across two packages.

    `cua.surface.snapshot` infers `Node.state.protected` from a control's accessible name
    while parsing aria-snapshot YAML; `cua.artifact.validate` matches the same rule against
    a persisted locator's `name` for spec §4.4's sixth condition. Both once compiled their
    own byte-identical regex over `PROTECTED_NAME_TOKENS`, with nothing holding the two
    equal -- and a drift between them is a credential leaking past one of the two checks
    that exist to stop it.

    This lives here, not beside the predicate, because it is a cross-package invariant: a
    test in `tests/surface/` that imports `cua.artifact` makes the phase-2 selection depend
    on phase-3 code, and that selection's count is a running invariant this project checks
    after every cross-phase change.

    The identity assertions are the load-bearing ones. Absence of a module-private regex
    only says a duplicate is not named `_PROTECTED_NAME_RE`; `is X` says each module still
    resolves the rule to the one shared object, which is what fails if either grows its own.
    """
    from cua.artifact import validate
    from cua.surface import models, snapshot

    assert not hasattr(snapshot, "_PROTECTED_NAME_RE")
    assert not hasattr(validate, "_PROTECTED_NAME_RE")
    assert not hasattr(snapshot, "_is_protected")
    assert snapshot.is_protected_name is models.is_protected_name
    assert validate.is_protected_name is models.is_protected_name


def test_the_failure_kind_vocabulary_has_exactly_one_implementation() -> None:
    """E4'/E29: one vocabulary, one membership set, one expect-code check, one path rule.

    `cua.artifact.validate` and `cua.replay.engine` each once carried a private
    `_FAILURE_KINDS = frozenset(get_args(FailureKind))` and a private `_permits_path`, with
    nothing holding the copies equal. The identity assertions are the load-bearing ones:
    `is` says each module still resolves the rule to the one shared object, which is what
    fails if either grows its own again.
    """
    from cua.artifact import models, validate
    from cua.replay import engine, result

    assert result.FailureKind is models.FailureKind
    assert validate.FAILURE_KINDS is models.FAILURE_KINDS
    assert engine.expect_code_problem is validate.expect_code_problem
    for module in (validate, engine):
        assert not hasattr(module, "_FAILURE_KINDS")
        assert not hasattr(module, "_permits_path")
        assert not hasattr(module, "_denied_navigation_reason")

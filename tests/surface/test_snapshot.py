from pathlib import Path

from cua.surface.models import SurfaceSegment
from cua.surface.snapshot import parse_aria_snapshot, scrub_protected_values

FIXTURE = Path("tests/fixtures/snapshots/search_frame.yaml").read_text()
PATH = [SurfaceSegment(kind="window", name="main"), SurfaceSegment(kind="frame", name="content")]

# The exact nested login-form YAML from the R12 probe: a password value leaking through both
# its own node's value and the accessible names of its enclosing cell and row.
LOGIN_YAML = """\
- table:
  - rowgroup:
    - row "Password teller-demo-pw":
      - cell "Password"
      - cell "teller-demo-pw":
        - textbox "Password": teller-demo-pw
"""

SECRET = "teller-demo-pw"

# Same protected subtree as LOGIN_YAML, but preceded by an unrelated sibling row/cell whose
# name coincidentally contains the secret substring, in its own subtree, before the protected
# node's subtree. This is the case that distinguishes ancestor-scoped scrubbing (only R12's
# actual ancestors get touched) from a global sweep (everything containing the substring gets
# touched, wrongly blanking unrelated page text).
LOGIN_YAML_WITH_UNRELATED_SIBLING = """\
- table:
  - rowgroup:
    - row "Shipping teller-demo-pw Lane":
      - cell "Shipping teller-demo-pw Lane"
    - row "Password teller-demo-pw":
      - cell "Password"
      - cell "teller-demo-pw":
        - textbox "Password": teller-demo-pw
"""


def nodes() -> list:
    return parse_aria_snapshot(FIXTURE, PATH)


def test_indices_are_dense_and_zero_based() -> None:
    assert [n.index for n in nodes()] == list(range(len(nodes())))


def test_every_node_carries_the_surface_path() -> None:
    assert all(n.surface_path == PATH for n in nodes())


def test_the_member_id_textbox_is_found_by_its_title_derived_name() -> None:
    hits = [n for n in nodes() if n.role == "textbox" and n.name == "Member ID"]
    assert len(hits) == 1


def test_the_search_button_is_found() -> None:
    assert any(n.role == "button" and n.name == "Search" for n in nodes())


def test_the_unnamed_input_is_present_with_an_empty_name() -> None:
    assert any(n.role == "textbox" and not n.name for n in nodes())


def test_a_protected_field_value_is_stripped_at_parse_time() -> None:
    # The phase 0 spike established that aria_snapshot() emits the live value of a password
    # input verbatim (decision log D11). Stripping it at parse time is the only point where the
    # value can be prevented from ever entering an Observation.
    leaky = 'textbox "PIN": hunter2\n'
    parsed = parse_aria_snapshot(leaky, PATH)
    pin = next(n for n in parsed if n.name == "PIN")
    assert pin.state.protected is True
    assert pin.value is None
    assert all("hunter2" not in (n.value or "") for n in parsed)


def test_ancestor_names_are_scrubbed_of_the_protected_value() -> None:
    parsed = parse_aria_snapshot(LOGIN_YAML, PATH)

    password_nodes = [n for n in parsed if n.role == "textbox" and n.name == "Password"]
    assert len(password_nodes) == 1
    assert password_nodes[0].state.protected is True
    assert password_nodes[0].value is None

    for n in parsed:
        assert SECRET not in (n.name or "")
        assert SECRET not in (n.value or "")

    row_nodes = [n for n in parsed if n.role == "row"]
    assert len(row_nodes) == 1
    assert "Password" in (row_nodes[0].name or "")

    serialized = "".join(str(n.model_dump()) for n in parsed)
    assert SECRET not in serialized


def test_ancestor_scrub_leaves_an_unrelated_preceding_sibling_intact() -> None:
    parsed = parse_aria_snapshot(LOGIN_YAML_WITH_UNRELATED_SIBLING, PATH)

    password_row = next(n for n in parsed if n.role == "row" and "Password" in (n.name or ""))
    assert SECRET not in (password_row.name or "")
    assert password_row.name == "Password"

    unrelated_row = next(n for n in parsed if n.role == "row" and "Shipping" in (n.name or ""))
    assert unrelated_row.name == "Shipping teller-demo-pw Lane"

    unrelated_cell = next(n for n in parsed if n.role == "cell" and "Shipping" in (n.name or ""))
    assert unrelated_cell.name == "Shipping teller-demo-pw Lane"


def test_word_boundary_matching_does_not_flag_unrelated_names() -> None:
    yaml_text = (
        '- textbox "Shipping address"\n'
        '- textbox "PIN"\n'
        '- textbox "Card CVV"\n'
    )
    parsed = parse_aria_snapshot(yaml_text, PATH)
    by_name = {n.name: n for n in parsed}
    assert by_name["Shipping address"].state.protected is False
    assert by_name["PIN"].state.protected is True
    assert by_name["Card CVV"].state.protected is True


def test_nested_node_depth_exceeds_its_containers() -> None:
    parsed = nodes()
    tables = [n for n in parsed if n.role == "table"]
    textboxes = [n for n in parsed if n.role == "textbox" and n.name == "Member ID"]
    assert tables
    assert textboxes
    assert textboxes[0].depth > tables[0].depth


def test_top_level_entries_are_depth_zero() -> None:
    # The fixture has two top-level `table` entries. A flat document-position counter would
    # give these 0 and 11 (or whatever their sequence index is); only true nesting depth
    # resets the second one back to 0. This is exactly what a mutation test caught: a
    # monotonically-increasing counter satisfies every other test in this file.
    parsed = nodes()
    tables = [n for n in parsed if n.role == "table"]
    assert len(tables) == 2
    assert all(n.depth == 0 for n in tables)


def test_a_known_nested_chain_has_exact_depths() -> None:
    # Exact values, not inequalities: table=0, rowgroup=1, row=2, cell=3, textbox=4, matching
    # the fixture's real accessibility-tree nesting rather than any monotonic proxy for it.
    parsed = nodes()
    table = next(n for n in parsed if n.role == "table")
    assert table.depth == 0
    rowgroup = next(n for n in parsed if n.role == "rowgroup")
    assert rowgroup.depth == 1
    row = next(n for n in parsed if n.role == "row" and n.name == "Member ID")
    assert row.depth == 2
    cell = next(n for n in parsed if n.role == "cell" and n.name == "Member ID")
    assert cell.depth == 3
    textbox = next(n for n in parsed if n.role == "textbox" and n.name == "Member ID")
    assert textbox.depth == 4


def test_a_url_property_line_does_not_become_a_node() -> None:
    yaml_text = '- link "Member Search":\n  - /url: /search\n'
    parsed = parse_aria_snapshot(yaml_text, PATH)
    assert len(parsed) == 1
    assert parsed[0].role == "link"
    assert parsed[0].name == "Member Search"


def test_scrub_protected_values_removes_the_secret_from_raw_yaml() -> None:
    scrubbed = scrub_protected_values(LOGIN_YAML)
    assert SECRET not in scrubbed
    assert "Password" in scrubbed
    assert "table" in scrubbed
    assert "rowgroup" in scrubbed


# S1: `yaml.safe_load` re-types a plain scalar (an octal-looking digit string, a bool word,
# a float) before the parser ever sees it as text, and `str()` on the re-typed Python value
# does not always round-trip to the original source text. Scrubbing must work against the
# value as it was written, not as re-stringified from a parsed scalar, or the secret survives
# verbatim in the row/cell names of the Observation and in the evidence YAML.
def test_octal_looking_secret_is_not_bypassed_by_yaml_int_coercion() -> None:
    # PyYAML 1.1 resolves a bare `0755` to the Python int 493; str(493) == "493", which does
    # not match the literal "0755" substring in the row name, so a naive str()-based scrub
    # misses it entirely.
    yaml_text = (
        '- row "PIN 0755":\n'
        '  - cell "PIN"\n'
        '  - textbox "PIN": 0755\n'
    )
    parsed = parse_aria_snapshot(yaml_text, PATH)
    row = next(n for n in parsed if n.role == "row")
    assert row.name == "PIN"
    pin = next(n for n in parsed if n.role == "textbox")
    assert pin.value is None


def test_bool_looking_secret_is_not_bypassed_by_yaml_bool_coercion() -> None:
    # PyYAML 1.1 resolves the bare word `off` to Python False; str(False) == "False", which
    # never appears in the row name at all.
    yaml_text = (
        '- row "PIN off":\n'
        '  - cell "PIN"\n'
        '  - textbox "PIN": off\n'
    )
    parsed = parse_aria_snapshot(yaml_text, PATH)
    row = next(n for n in parsed if n.role == "row")
    assert row.name == "PIN"
    pin = next(n for n in parsed if n.role == "textbox")
    assert pin.value is None


def test_float_looking_secret_is_not_partially_scrubbed_by_yaml_float_coercion() -> None:
    # str(1.5) == "1.5" -- the trailing zero from "1.50" is silently dropped, so a naive
    # scrub of "1.5" out of "PIN 1.50" leaves a dangling "PIN 0" behind.
    yaml_text = (
        '- row "PIN 1.50":\n'
        '  - cell "PIN"\n'
        '  - textbox "PIN": 1.50\n'
    )
    parsed = parse_aria_snapshot(yaml_text, PATH)
    row = next(n for n in parsed if n.role == "row")
    assert row.name == "PIN"
    pin = next(n for n in parsed if n.role == "textbox")
    assert pin.value is None


# S2: the protected node's own name was never scrubbed -- only its ancestors were. A label
# that carries the value inline (a hostile but real shape: `textbox "Password hunter2"`)
# must not leak the value through the node's own `name` field.
def test_a_protected_nodes_own_name_is_scrubbed_of_its_value() -> None:
    yaml_text = 'textbox "Password hunter2": hunter2\n'
    parsed = parse_aria_snapshot(yaml_text, PATH)
    node = parsed[0]
    assert node.state.protected is True
    assert node.value is None
    assert "hunter2" not in (node.name or "")


# S3: a global textual replace with the empty string does not just remove the secret, it
# corrupts unrelated content that happens to share the same substring -- a legitimate
# `button "Search"` becomes `button ""` when the password is "Search", and a one-character
# password mangles role names outright (`textbox` -> `txtbox` for password "e"). A visible
# redaction marker means a reader can tell scrubbing happened instead of silently losing or
# corrupting unrelated text.
def test_scrub_protected_values_uses_a_visible_marker_not_a_blank() -> None:
    yaml_text = (
        '- row "Password Search":\n'
        '  - cell "Password"\n'
        '  - textbox "Password": Search\n'
        '- button "Search"\n'
    )
    scrubbed = scrub_protected_values(yaml_text)
    assert "Search" not in scrubbed
    assert "[REDACTED]" in scrubbed
    assert 'button ""' not in scrubbed


def test_scrub_protected_values_does_not_mangle_role_names_for_a_short_secret() -> None:
    yaml_text = '- textbox "Password": e\n'
    scrubbed = scrub_protected_values(yaml_text)
    assert "txtbox" not in scrubbed
    assert "[REDACTED]" in scrubbed


# R3, widened: protection is inferred from the accessible name alone (PROTECTED_NAME_TOKENS
# matched against `name`), so a password field with NO name at all carries no signal a
# parser could use -- not just a field with an unusual label. Pinned deliberately so the next
# reader meets this gap on purpose rather than discovering it by leaking a credential.
def test_an_unnamed_password_field_is_not_protected_a_known_blind_spot() -> None:
    yaml_text = '- textbox: hunter2\n'
    parsed = parse_aria_snapshot(yaml_text, PATH)
    node = parsed[0]
    assert node.name is None
    assert node.state.protected is False
    assert node.value == "hunter2"

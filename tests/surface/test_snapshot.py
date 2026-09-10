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

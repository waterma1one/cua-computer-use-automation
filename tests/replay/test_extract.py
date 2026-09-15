import pytest

from cua.replay.extract import ParseError, extract
from cua.surface.models import Node, NodeState, SurfaceSegment

PATH = [SurfaceSegment(kind="window", name="main")]


def node(name: str | None = None, value: str | None = None) -> Node:
    return Node(index=0, role="cell", name=name, value=value, state=NodeState(),
                surface_path=PATH)


def test_raw_extraction_returns_the_node_text_unparsed() -> None:
    assert extract(node(value="  4,218.60 "), "value", "raw") == "  4,218.60 "


def test_text_extraction_reads_the_accessible_name() -> None:
    assert extract(node(name="Member 12345"), "text", "raw") == "Member 12345"


def test_money_parses_a_comma_formatted_balance() -> None:
    assert extract(node(value="4,218.60"), "value", "money") == "4218.60"


def test_money_strips_a_dollar_sign() -> None:
    assert extract(node(value="$1,234.56"), "value", "money") == "1234.56"


def test_a_non_numeric_money_value_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        extract(node(value="please try again"), "value", "money")


def test_money_rejects_european_formatted_balance() -> None:
    with pytest.raises(ParseError):
        extract(node(value="1.234,56"), "value", "money")


def test_money_rejects_scientific_notation() -> None:
    with pytest.raises(ParseError):
        extract(node(value="1e3"), "value", "money")


def test_money_rejects_nan() -> None:
    with pytest.raises(ParseError):
        extract(node(value="nan"), "value", "money")


def test_money_rejects_underscores() -> None:
    with pytest.raises(ParseError):
        extract(node(value="1_000.50"), "value", "money")


def test_money_rejects_more_than_two_decimals() -> None:
    with pytest.raises(ParseError):
        extract(node(value="12.345"), "value", "money")


def test_money_accepts_negative_values() -> None:
    assert extract(node(value="-12.50"), "value", "money") == "-12.50"


def test_money_formats_integers_with_two_decimals() -> None:
    assert extract(node(value="1,234"), "value", "money") == "1234.00"


def test_money_raises_parse_error_on_none_value() -> None:
    with pytest.raises(ParseError):
        extract(node(value=None), "value", "money")


def test_int_parses_a_plain_integer_string() -> None:
    assert extract(node(value="42"), "value", "int") == 42


def test_a_non_integer_value_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        extract(node(value="not a number"), "value", "int")


def test_date_parses_an_iso_date() -> None:
    assert extract(node(value="2026-09-09"), "value", "date") == "2026-09-09"


def test_an_unparseable_date_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        extract(node(value="whenever"), "value", "date")


def test_attribute_extraction_is_not_yet_supported() -> None:
    with pytest.raises(NotImplementedError):
        extract(node(name="x"), "attribute", "raw")

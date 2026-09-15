"""Extract and parse values from accessibility snapshot nodes.

Reads named and valued fields from `Node` objects (accessible names, node values),
validates them against expected shapes (money, integers, dates), and returns
validated output or raises `ParseError` when a value cannot be parsed.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from cua.surface.models import Node

__all__ = ["ParseError", "extract"]


class ParseError(ValueError):
    """Raised when a value cannot be parsed into the requested format."""


def _raw_text(
    node: Node,
    extract: Literal["text", "value", "attribute"],
) -> str:
    """Extract unvalidated text from a node.

    Reads `node.name` for "text", `node.value` for "value", and raises
    `NotImplementedError` for "attribute" since `Node` has no generic
    attribute bag.
    """
    if extract == "text":
        text = node.name
    elif extract == "value":
        text = node.value
    else:  # extract == "attribute"
        raise NotImplementedError("attribute extraction is not yet supported")

    return text if text is not None else ""


def _parse_money(text: str) -> str:
    r"""Parse a money string and return canonical form with exactly two decimals.

    Strips $, commas, and surrounding whitespace. Rejects anything not shaped
    like a plain decimal (matches -?\d+(\.\d{1,2})?). Returns a string with
    exactly two decimal places. Negatives are allowed (overdrawn balances).
    """
    # Strip dollar sign and whitespace
    cleaned = text.strip().lstrip("$").strip()

    # Remove commas
    cleaned = cleaned.replace(",", "")

    # Validate format: optional minus, digits, optional dot and 1-2 decimals
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", cleaned):
        raise ParseError(f"cannot parse '{text}' as money")

    # Use Decimal for precise formatting without float rounding issues
    try:
        decimal_value = Decimal(cleaned)
        # Format to exactly 2 decimal places
        return str(decimal_value.quantize(Decimal("0.01")))
    except Exception as err:
        raise ParseError(f"cannot parse '{text}' as money") from err


def _parse_int(text: str) -> int:
    """Parse an integer string, stripping commas and whitespace.

    Raises `ParseError` if the value is not a valid integer.
    """
    # Strip whitespace and remove commas
    cleaned = text.strip().replace(",", "")

    try:
        return int(cleaned)
    except ValueError as err:
        raise ParseError(f"cannot parse '{text}' as integer") from err


def _parse_date(text: str) -> str:
    """Parse an ISO date string and return it back out.

    Tries `date.fromisoformat` and raises `ParseError` on failure.
    Returns the ISO date string.
    """
    try:
        # Validate by parsing as a date
        date.fromisoformat(text.strip())
        # Return the ISO string (stripped)
        return text.strip()
    except ValueError as err:
        raise ParseError(f"cannot parse '{text}' as date") from err


def extract(
    node: Node,
    extract: Literal["text", "value", "attribute"],
    parse: Literal["raw", "money", "int", "date"] | None,
) -> object:
    """Extract and optionally parse a value from a node.

    Reads the node's accessible name (extract="text"), node value
    (extract="value"), or raises NotImplementedError (extract="attribute").
    Optionally parses the extracted text according to `parse`:
    - "raw": returns the text unparsed
    - "money": parses as currency with exactly 2 decimal places
    - "int": parses as an integer
    - "date": parses as an ISO date string
    """
    text = _raw_text(node, extract)

    if parse is None or parse == "raw":
        return text
    elif parse == "money":
        return _parse_money(text)
    elif parse == "int":
        return _parse_int(text)
    elif parse == "date":
        return _parse_date(text)
    else:
        raise ValueError(f"unknown parse mode: {parse}")

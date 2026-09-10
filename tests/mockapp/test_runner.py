"""Tests for the variant runner (`python -m mockapp <variant>`).

Starting a live server is not exercised here -- that is verified by hand per the task
brief. What is real logic, and therefore tested, is: which variant and port a given
argv resolves to, and what happens when the target port is already occupied (the
runner must report it plainly and exit nonzero, not hang or dump a traceback).
"""

from __future__ import annotations

import socket

import pytest

from mockapp import __main__ as runner


def test_no_argument_selects_the_base_variant_on_its_fixed_port() -> None:
    assert runner.resolve_variant_and_port(["mockapp"]) == ("base", runner.BASE_PORT)


def test_explicit_base_argument_selects_the_base_variant() -> None:
    assert runner.resolve_variant_and_port(["mockapp", "base"]) == ("base", runner.BASE_PORT)


def test_b_argument_selects_variant_b_on_its_own_fixed_port() -> None:
    assert runner.resolve_variant_and_port(["mockapp", "b"]) == ("b", runner.VARIANT_B_PORT)


def test_variant_ports_are_distinct() -> None:
    assert runner.BASE_PORT != runner.VARIANT_B_PORT


def test_main_reports_an_unknown_variant_without_crashing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = runner.main(["mockapp", "bas"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "unknown variant 'bas'" in err, "the message must name what was passed"
    assert "base, b" in err, "the message must name the valid variants"


def test_main_refuses_to_start_and_names_the_port_when_it_is_already_taken(
    capsys: pytest.CaptureFixture[str],
) -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", runner.BASE_PORT))
    occupied.listen(1)
    try:
        exit_code = runner.main(["mockapp", "base"])
    finally:
        occupied.close()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert str(runner.BASE_PORT) in err, "the message must name the actual port"
    assert "already in use" in err

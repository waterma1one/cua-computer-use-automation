"""Runner that serves either mockapp variant on its own fixed port.

    .venv/bin/python -m mockapp        # variant "base" on 127.0.0.1:8811
    .venv/bin/python -m mockapp b      # variant "b"    on 127.0.0.1:8812

No environment variable is required to start either variant; every config knob
(`MOCKAPP_LOGIN_USER`, `MOCKAPP_SESSION_MAX_REQUESTS`, `MOCKAPP_FAULT`,
`MOCKAPP_SLOW_FAULT_MS`) has a working default, per `.env.example`.

Before binding, the target port is probed with a throwaway socket. If it is already
taken, the runner prints one line naming the port and variant and exits with status 1
instead of letting uvicorn's own bind failure surface as a buried log line or, if
something upstream were ever changed, a raw traceback. This is a best-effort check
(there is an unavoidable, brief race between the probe and the real bind further down
in uvicorn) -- good enough for a local dev runner, not a guarantee under contention.

An argv value other than "base" or "b" is rejected the same way, before `create_app`
is ever called: one line to stderr naming what was passed and what is valid, exit
status 1. Without this check, a typo (`python -m mockapp bas`) would reach
`create_app`'s `BRANDS[variant]` lookup and crash with an unhandled `KeyError`.
"""

from __future__ import annotations

import socket
import sys

import uvicorn

from mockapp.app import create_app

HOST = "127.0.0.1"
BASE_PORT = 8811
VARIANT_B_PORT = 8812
KNOWN_VARIANTS = ("base", "b")


def resolve_variant_and_port(argv: list[str]) -> tuple[str, int]:
    """Pick the variant and its fixed port from argv (argv[0] is the program name).

    Any argument other than "base" is routed to the non-base port; today that means
    "b", the only other variant `create_app` knows about. This function does not
    validate the variant itself -- an argv value outside `KNOWN_VARIANTS` still
    resolves here (to the "b" port) but is rejected by `main` before `create_app` is
    ever called, so the unhandled `KeyError` `create_app`'s own `BRANDS[variant]`
    lookup would otherwise raise never happens.
    """
    variant = argv[1] if len(argv) > 1 else "base"
    port = BASE_PORT if variant == "base" else VARIANT_B_PORT
    return variant, port


def _port_is_free(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def main(argv: list[str] | None = None) -> int:
    variant, port = resolve_variant_and_port(sys.argv if argv is None else argv)

    if variant not in KNOWN_VARIANTS:
        print(
            f"mockapp: unknown variant {variant!r} "
            f"(expected one of: {', '.join(KNOWN_VARIANTS)}).",
            file=sys.stderr,
        )
        return 1

    if not _port_is_free(HOST, port):
        print(
            f"mockapp: port {port} is already in use (needed for variant {variant!r}). "
            "Stop whatever is listening on it, or free the port, and try again.",
            file=sys.stderr,
        )
        return 1

    uvicorn.run(create_app(variant), host=HOST, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

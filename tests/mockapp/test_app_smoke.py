import asyncio

import httpx
from fastapi import FastAPI

from mockapp.app import create_app


class _SyncASGITransport(httpx.BaseTransport):
    """Drive an ASGI app synchronously.

    Deviation from the brief: the installed httpx (0.28.1) implements
    ASGITransport with only `handle_async_request`, so the brief's plain
    `httpx.Client(transport=httpx.ASGITransport(app=...))` raises
    `AttributeError: 'ASGITransport' object has no attribute 'handle_request'`.
    This wraps the async transport in a synchronous one via `asyncio.run`,
    which keeps the rest of the test file, including every assertion, exactly
    as written in the brief.
    """

    def __init__(self, app: FastAPI) -> None:
        self._async_transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def run() -> httpx.Response:
            response = await self._async_transport.handle_async_request(request)
            body = await response.aread()
            await response.aclose()
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=body,
                request=request,
            )

        return asyncio.run(run())


def client(variant: str = "base") -> httpx.Client:
    return httpx.Client(transport=_SyncASGITransport(create_app(variant)),
                        base_url="http://app")


def test_root_is_a_classic_frameset() -> None:
    body = client().get("/").text.lower()
    assert "<frameset" in body, "the surface must be a real frameset, not iframes"
    assert 'name="content"' in body
    assert 'name="nav"' in body


def test_frameset_names_are_stable_addresses() -> None:
    body = client().get("/").text
    # The surface layer addresses frames by name, so these are part of the contract.
    assert 'name="content"' in body and 'name="nav"' in body

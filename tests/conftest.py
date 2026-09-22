"""Общие тестовые хелперы: единый локальный сервер и запуск сценариев."""

import asyncio
from collections.abc import Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

BODY = b"x" * 2048


def build_app() -> web.Application:
    """Собирает приложение: /ok, /perm, /retry, /rate (+Retry-After), /short."""
    app = web.Application()

    async def ok_handler(request: web.Request) -> web.Response:
        return web.Response(body=BODY)

    async def permanent_handler(request: web.Request) -> web.Response:
        return web.Response(status=400)

    async def retryable_handler(request: web.Request) -> web.Response:
        return web.Response(status=500)

    async def rate_limited_handler(request: web.Request) -> web.Response:
        return web.Response(status=429, headers={"Retry-After": "2"})

    async def short_read_handler(request: web.Request) -> web.Response:
        response = web.StreamResponse(headers={"Content-Length": "100"})
        await response.prepare(request)
        await response.write(b"abc")
        request.transport.close()
        return response

    app.router.add_get("/ok", ok_handler)
    app.router.add_get("/perm", permanent_handler)
    app.router.add_get("/retry", retryable_handler)
    app.router.add_get("/rate", rate_limited_handler)
    app.router.add_get("/short", short_read_handler)
    return app


@pytest.fixture
def body() -> bytes:
    """Тело, которое отдаёт тестовый сервер (для сверки размеров в сценариях)."""
    return BODY


@pytest.fixture
def app() -> web.Application:
    """Тестовое приложение для самодельных запусков сервера (например, в main)."""
    return build_app()


@pytest.fixture
def run_with_server() -> Callable[..., object]:
    """Фабрика запуска async-сценария ``scenario(session, url)`` на сервере."""

    def _run(
        scenario: Callable[[aiohttp.ClientSession, str], Awaitable],
        path: str,
    ) -> object:
        async def runner() -> object:
            server = TestServer(build_app())
            await server.start_server()
            try:
                url = str(server.make_url(path))
                async with aiohttp.ClientSession() as session:
                    return await scenario(session, url)
            finally:
                await server.close()

        return asyncio.run(runner())

    return _run

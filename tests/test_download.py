"""Скачивание через локальный aiohttp.TestServer — без внешней сети."""

import asyncio
import email.utils
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from speedtest.core import (
    RetryableDownloadError,
    _core_download,
    _parse_retry_after,
    run_adaptive_benchmark,
)

BODY = b"x" * 2048


def build_app() -> web.Application:
    """Собирает приложение с маршрутами: ок, 400, 500, 429+Retry-After, short read."""
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


def run_with_server(
    scenario: Callable[[aiohttp.ClientSession, str], Awaitable],
    path: str,
) -> object:
    """Запускает async-сценарий с локальным тестовым сервером."""

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


def test_full_download_succeeds():
    """Успешная закачка: все байты получены, ошибок нет."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        result = await _core_download(session, url, 5.0, 5.0)
        assert result.is_ok is True
        assert result.bytes_downloaded == len(BODY)
        assert result.error is None

    run_with_server(scenario, "/ok")


def test_permanent_http_status_returns_failure_without_retry():
    """Постоянный 4xx возвращается как результат-ошибка, а не исключение."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        result = await _core_download(session, url, 5.0, 5.0)
        assert result.is_ok is False
        assert result.error == "HTTP 400"

    run_with_server(scenario, "/perm")


def test_retryable_http_status_raises():
    """Временные 5xx возбуждают RetryableDownloadError."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        await _core_download(session, url, 5.0, 5.0)

    with pytest.raises(RetryableDownloadError, match="HTTP 500"):
        run_with_server(scenario, "/retry")


def test_short_read_raises_client_error():
    """Недокачанное тело возбуждает aiohttp.ClientError (короткое чтение)."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        await _core_download(session, url, 5.0, 5.0)

    with pytest.raises(aiohttp.ClientError):
        run_with_server(scenario, "/short")


def test_rate_limit_carries_retry_after():
    """429 с Retry-After: исключение хранит секунды ожидания."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        await _core_download(session, url, 5.0, 5.0)

    with pytest.raises(RetryableDownloadError, match="HTTP 429") as exc_info:
        run_with_server(scenario, "/rate")
    assert exc_info.value.retry_after == 2.0


def test_parse_retry_after_seconds():
    """Delta-seconds из Retry-After переводится в число секунд."""
    assert _parse_retry_after("5") == 5.0


def test_parse_retry_after_http_date():
    """HTTP-date из Retry-After даёт оставшиеся секунды до этой даты."""
    value = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=7))
    retry_after = _parse_retry_after(value)
    assert 6.0 <= retry_after <= 8.0


def test_parse_retry_after_garbage():
    """Мусор, пустая строка и None разбираются как None."""
    assert _parse_retry_after("not-a-date") is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after(None) is None


def test_full_benchmark_over_local_server():
    """Полный замер на локальном сервере отдаёт все результаты и агрегаты."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        outcome = await run_adaptive_benchmark(
            session,
            url,
            requests=2,
            start_cwnd=1,
            attempts=3,
            read_timeout=5.0,
            connect_timeout=5.0,
        )
        assert len(outcome.results) == 2
        assert all(result.is_ok for result in outcome.results)
        assert outcome.cwnd_start == 1
        assert outcome.stable_bytes == 2 * len(BODY)
        assert outcome.stable_wall_seconds > 0

    run_with_server(scenario, "/ok")


def test_parallel_wave_uses_wall_span_not_sum_of_durations():
    """Агрегат меняется по span, а не сумме длительностей параллельной волны."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        outcome = await run_adaptive_benchmark(
            session,
            url,
            requests=4,
            start_cwnd=4,
            attempts=2,
            read_timeout=5.0,
            connect_timeout=5.0,
        )
        assert len(outcome.results) == 4
        assert all(result.is_ok for result in outcome.results)
        durations = [result.duration_seconds for result in outcome.results]
        # 4 закачки летят параллельно: span ≪ сумма индивидуальных длительностей.
        assert outcome.stable_wall_seconds * 2 < sum(durations)
        assert outcome.stable_bytes == 4 * len(BODY)

    run_with_server(scenario, "/ok")


def test_sum_stable_waves_from_halfway():
    """Суммирование стабильных волн начинается с волны, где >= stable_min_request_id."""
    from speedtest.core import _sum_stable_waves

    total_bytes, total_wall = _sum_stable_waves(
        wave_bytes=[100, 200, 300],
        wave_wall_seconds=[1.0, 2.0, 3.0],
        wave_start_request_ids=[0, 2, 5],
        stable_min_request_id=2,
    )
    assert total_bytes == 500
    assert total_wall == 5.0

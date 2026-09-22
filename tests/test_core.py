"""Граничные случаи core: ретраи, wait-логика, run_wave с ошибкой, цикл замера."""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import speedtest.core as core
from speedtest.core import (
    AimdStep,
    RetryableDownloadError,
    _parse_retry_after,
    _wait_for_retry_after,
    make_download_once,
    run_adaptive_benchmark,
    run_wave,
)

BODY = b"z" * 4096


def build_app() -> web.Application:
    """Собирает приложение с маршрутами для сценариев ретраев."""
    app = web.Application()

    async def retryable_handler(request: web.Request) -> web.Response:
        return web.Response(status=500)

    async def permanent_handler(request: web.Request) -> web.Response:
        return web.Response(status=400)

    async def ok_handler(request: web.Request) -> web.Response:
        return web.Response(body=BODY)

    app.router.add_get("/retry", retryable_handler)
    app.router.add_get("/perm", permanent_handler)
    app.router.add_get("/ok", ok_handler)
    return app


def run_with_server(
    scenario: Callable[[object, str], Awaitable],
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


def test_parse_retry_after_naive_http_date():
    """HTTP-date без таймзоны приводится к UTC и уходит в прошлое как 0."""
    assert _parse_retry_after("Wed, 01 Jan 1990 12:00:00") == 0.0


def test_parse_retry_after_parsedate_returns_none(monkeypatch):
    """Парсер возвращает None, если модуль парсинга вернул None."""
    monkeypatch.setattr("email.utils.parsedate_to_datetime", lambda value: None)
    assert _parse_retry_after("anything") is None


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (5.0, 5.0),
        (0.0, 1.0),
        (100.0, 15.0),
    ],
)
def test_wait_for_retry_after_uses_header(retry_after, expected):
    """Retry-After честно учитывается и зажимается в диапазон [1, 15] секунд."""
    state = SimpleNamespace(
        outcome=SimpleNamespace(
            exception=lambda: RetryableDownloadError("HTTP 429", retry_after)
        )
    )
    assert _wait_for_retry_after(state) == expected


def test_wait_for_retry_after_falls_back_to_exponential(monkeypatch):
    """Без Retry-After используется экспоненциальный backoff от tenacity."""
    monkeypatch.setattr(
        core, "wait_exponential_jitter", lambda *a, **k: lambda state: 999.0
    )
    state = SimpleNamespace(outcome=SimpleNamespace(exception=lambda: OSError("boom")))
    assert _wait_for_retry_after(state) == 999.0


def test_retries_exhausted_return_failed_result(monkeypatch):
    """После исчерпания попыток возвращается DownloadResult с ошибкой."""
    monkeypatch.setattr(
        core, "wait_exponential_jitter", lambda *a, **k: lambda state: 0.0
    )

    async def scenario(session: object, url: str):
        download_once = make_download_once(3, 5.0, 5.0)
        result = await download_once(session, url)
        assert result.is_ok is False
        assert result.error == "RetryableDownloadError: HTTP 500"
        assert result.retry_count == 3

    run_with_server(scenario, "/retry")


def test_run_wave_converts_unexpected_exception():
    """Неретраимые исключения из задачи превращаются в DownloadResult-фейл."""

    class StubSession:
        def get(self, url: str, timeout: object):
            raise ValueError("boom")

    results = asyncio.run(run_wave(StubSession(), "http://x", 1, 1, 3, 5.0, 5.0))
    assert len(results) == 1
    assert results[0].is_ok is False
    assert results[0].error == "ValueError('boom')"


def test_benchmark_rejects_zero_requests():
    """Замер с requests < 1 падает с ValueError ещё до первого запроса."""
    with pytest.raises(ValueError, match="requests"):
        asyncio.run(run_adaptive_benchmark(object(), "http://x", 0, 1, 1, 1.0, 1.0))


def test_benchmark_all_fail_falls_back_to_first_wave():
    """При тотальных фейлах стабильная фаза начинается с первой волны."""

    async def scenario(session: object, url: str):
        outcome = await run_adaptive_benchmark(
            session,
            url,
            requests=2,
            start_cwnd=1,
            attempts=1,
            read_timeout=5.0,
            connect_timeout=5.0,
        )
        assert outcome.stable_min_request_id == 1
        assert outcome.stable_bytes == 0
        assert outcome.stable_wall_seconds == 0.0

    run_with_server(scenario, "/perm")


def test_benchmark_plateau_sets_stable_min_inside_loop(monkeypatch):
    """Плато-волна фиксирует стебильную фазу с её первого запроса."""

    def fake_aimd(cwnd, wave_tp, ewma_prev, error_ratio, max_cwnd, best_cwnd):
        return AimdStep(cwnd, 1.0, False, False)

    monkeypatch.setattr(core, "aimd_next", fake_aimd)

    async def scenario(session: object, url: str):
        outcome = await run_adaptive_benchmark(
            session,
            url,
            requests=2,
            start_cwnd=1,
            attempts=1,
            read_timeout=5.0,
            connect_timeout=5.0,
        )
        assert outcome.stable_min_request_id == 1
        assert outcome.stable_bytes == 2 * len(BODY)

    run_with_server(scenario, "/ok")


def test_benchmark_calls_on_wave_callback(monkeypatch):
    """Колбэк on_wave получает сводку каждой волны в порядке запуска."""

    def fake_aimd(cwnd, wave_tp, ewma_prev, error_ratio, max_cwnd, best_cwnd):
        return AimdStep(max(1, cwnd // 2), 1.0, False, True)

    monkeypatch.setattr(core, "aimd_next", fake_aimd)
    waves: list[object] = []

    async def scenario(session: object, url: str):
        await run_adaptive_benchmark(
            session,
            url,
            requests=3,
            start_cwnd=2,
            attempts=1,
            read_timeout=5.0,
            connect_timeout=5.0,
            on_wave=waves.append,
        )

    run_with_server(scenario, "/ok")

    assert len(waves) == 2
    first, second = waves
    assert first.wave_index == 1
    assert first.done_requests == 2
    assert first.total_requests == 3
    assert first.ok_count == 2
    assert first.fail_count == 0
    assert first.cwnd == 2
    assert first.next_cwnd == 1
    assert first.is_degraded is True
    assert second.wave_index == 2
    assert second.done_requests == 3
    assert second.ok_count == 1

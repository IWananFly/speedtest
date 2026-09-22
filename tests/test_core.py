"""Граничные случаи core: ретраи, wait-логика, run_wave с ошибкой, цикл замера."""

import asyncio
from types import SimpleNamespace

import pytest

from speedtest import core
from speedtest.core import (
    AimdStep,
    RetryableDownloadError,
    _parse_retry_after,
    _wait_for_retry_after,
    make_download_once,
    run_adaptive_benchmark,
    run_wave,
)


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


def test_retries_exhausted_return_failed_result(monkeypatch, run_with_server):
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


def test_benchmark_rejects_zero_attempts():
    """Замер с attempts < 1 падает с ValueError ещё до первого запроса."""
    with pytest.raises(ValueError, match="attempts"):
        asyncio.run(run_adaptive_benchmark(object(), "http://x", 1, 1, 0, 1.0, 1.0))


def test_benchmark_all_fail_falls_back_to_first_wave(run_with_server):
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


def test_benchmark_plateau_sets_stable_min_inside_loop(
    monkeypatch, run_with_server, body
):
    """Плато-волна фиксирует стабильную фазу с её первого запроса."""

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
        assert outcome.stable_bytes == 2 * len(body)

    run_with_server(scenario, "/ok")


def test_benchmark_plateau_after_ramp_excludes_slow_start(
    monkeypatch, run_with_server, body
):
    """Плато со второй волны исключает slow-start первую волну из агрегата."""
    steps = iter(
        [
            AimdStep(2, 1.0, True, False),  # волна 1: разгон cwnd 1→2
            AimdStep(2, 1.0, False, False),  # волна 2: плато → стабильная фаза
        ]
    )
    monkeypatch.setattr(core, "aimd_next", lambda *args: next(steps))

    async def scenario(session: object, url: str):
        outcome = await run_adaptive_benchmark(
            session,
            url,
            requests=3,
            start_cwnd=1,
            attempts=1,
            read_timeout=5.0,
            connect_timeout=5.0,
        )
        assert outcome.stable_min_request_id == 2
        assert outcome.stable_bytes == 2 * len(body)

    run_with_server(scenario, "/ok")


def test_benchmark_calls_on_wave_callback(monkeypatch, run_with_server):
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

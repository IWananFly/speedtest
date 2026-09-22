"""Скачивание через локальный aiohttp.TestServer — без внешней сети."""

import email.utils
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest

from speedtest.core import (
    RetryableDownloadError,
    _core_download,
    _parse_retry_after,
    run_adaptive_benchmark,
)


def test_full_download_succeeds(run_with_server, body):
    """Успешная закачка: все байты получены, ошибок нет."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        result = await _core_download(session, url, 5.0, 5.0)
        assert result.is_ok is True
        assert result.bytes_downloaded == len(body)
        assert result.error is None

    run_with_server(scenario, "/ok")


def test_permanent_http_status_returns_failure_without_retry(run_with_server):
    """Постоянный 4xx возвращается как результат-ошибка, а не исключение."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        result = await _core_download(session, url, 5.0, 5.0)
        assert result.is_ok is False
        assert result.error == "HTTP 400"

    run_with_server(scenario, "/perm")


def test_retryable_http_status_raises(run_with_server):
    """Временные 5xx возбуждают RetryableDownloadError."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        await _core_download(session, url, 5.0, 5.0)

    with pytest.raises(RetryableDownloadError, match="HTTP 500"):
        run_with_server(scenario, "/retry")


def test_short_read_raises_client_error(run_with_server):
    """Недокачанное тело возбуждает aiohttp.ClientError (короткое чтение)."""

    async def scenario(session: aiohttp.ClientSession, url: str):
        await _core_download(session, url, 5.0, 5.0)

    with pytest.raises(aiohttp.ClientError):
        run_with_server(scenario, "/short")


def test_rate_limit_carries_retry_after(run_with_server):
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


def test_full_benchmark_over_local_server(run_with_server, body):
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
        assert outcome.stable_bytes == 2 * len(body)
        assert outcome.stable_wall_seconds > 0

    run_with_server(scenario, "/ok")


def test_parallel_wave_uses_wall_span_not_sum_of_durations(run_with_server, body):
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
        # 4 закачки летят параллельно: span ≪ суммы индивидуальных длительностей.
        assert outcome.stable_wall_seconds * 2 < sum(durations)
        assert outcome.stable_bytes == 4 * len(body)

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

"""Скачивание, онлайн-AIMD адаптация и расчёты скорости.

Модуль не зависит от CLI: чистые функции и данные на вход/выход,
вся I/O-работа — асинхронная. Отрисовка отчётов живёт в ``cli``.
"""

import asyncio
import email.utils
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import aiohttp
from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

CHUNK_SIZE = 64 * 1024
AI_IMPROVEMENT_FACTOR = 1.10
DEGRADE_FACTOR = 0.90
MD_ERROR_RATIO = 0.2
EWMA_ALPHA = 0.5
MD_FACTOR = 2
MAX_CONCURRENCY = 32

logger = logging.getLogger("speedtest")


RETRY_WAIT_CAP_SECONDS = 15.0


class RetryableDownloadError(Exception):
    """Временная ошибка скачивания, которую стоит ретраить."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    """Секунды ожидания из заголовка Retry-After (delta-seconds или HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(int(value))
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _wait_for_retry_after(retry_state: RetryCallState) -> float:
    """Ждём Retry-After сервера, иначе экспоненциальный backoff с джиттером."""
    exception = retry_state.outcome.exception()
    if (
        isinstance(exception, RetryableDownloadError)
        and exception.retry_after is not None
    ):
        return min(RETRY_WAIT_CAP_SECONDS, max(1.0, exception.retry_after))
    return wait_exponential_jitter(initial=0.5, max=5)(retry_state)


RETRYABLE_HTTP_STATUSES = {408, 429, *range(500, 600)}
PERMANENT_HTTP_STATUSES = {*range(400, 500)} - RETRYABLE_HTTP_STATUSES
RETRYABLE_EXCEPTIONS = (
    RetryableDownloadError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
)

# Изоляция счётчика по контексту каждой задачи в asyncio.gather: без неё
# параллельные закачки одной волны затирали бы попытки друг друга.
_attempt_counter: ContextVar[int] = ContextVar("download_attempt", default=0)


@dataclass
class DownloadResult:
    request_id: int
    bytes_downloaded: int
    duration_seconds: float
    is_ok: bool
    error: str | None
    retry_count: int = 0


@dataclass(frozen=True)
class AimdStep:
    new_cwnd: int
    ewma: float
    is_improved: bool
    is_degraded: bool = False


@dataclass(frozen=True)
class BenchmarkOutcome:
    results: Sequence[DownloadResult]
    stable_min_request_id: int
    cwnd_start: int
    cwnd_max: int
    stable_bytes: int
    stable_wall_seconds: float
    total_wall_seconds: float


def _describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _to_failed_result(retry_state: RetryCallState) -> DownloadResult:
    exc = retry_state.outcome.exception()
    error = _describe_error(exc) if exc else "retries exhausted"
    return DownloadResult(
        request_id=0,
        bytes_downloaded=0,
        duration_seconds=0.0,
        is_ok=False,
        error=error,
        retry_count=max(1, _attempt_counter.get()),
    )


async def _core_download(
    session: aiohttp.ClientSession,
    url: str,
    read_timeout: float,
    connect_timeout: float,
) -> DownloadResult:
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=connect_timeout,
        sock_connect=connect_timeout,
        sock_read=read_timeout,
    )
    start = time.perf_counter()
    size = 0
    content_length: int | None = None
    async with session.get(url, timeout=timeout) as resp:
        if resp.status in PERMANENT_HTTP_STATUSES:
            return DownloadResult(0, 0, 0.0, False, f"HTTP {resp.status}")
        if resp.status in RETRYABLE_HTTP_STATUSES:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            raise RetryableDownloadError(f"HTTP {resp.status}", retry_after)
        content_length = resp.content_length
        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
            size += len(chunk)
    elapsed = time.perf_counter() - start
    if content_length is not None and size != content_length:
        raise RetryableDownloadError(
            f"short read: got {size} of {content_length} bytes"
        )
    return DownloadResult(0, size, elapsed, True, None)


def make_download_once(
    attempts: int,
    read_timeout: float,
    connect_timeout: float,
) -> Callable[[aiohttp.ClientSession, str], Awaitable[DownloadResult]]:
    @retry(
        stop=stop_after_attempt(attempts),
        wait=_wait_for_retry_after,
        retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        retry_error_callback=_to_failed_result,
    )
    async def download(session: aiohttp.ClientSession, url: str) -> DownloadResult:
        _attempt_counter.set(_attempt_counter.get() + 1)
        result = await _core_download(session, url, read_timeout, connect_timeout)
        result.retry_count = max(1, _attempt_counter.get())
        return result

    return download


async def run_wave(
    session: aiohttp.ClientSession,
    url: str,
    concurrency: int,
    count: int,
    attempts: int,
    read_timeout: float,
    connect_timeout: float,
) -> list[DownloadResult]:
    download_once = make_download_once(attempts, read_timeout, connect_timeout)
    semaphore = asyncio.Semaphore(concurrency)

    async def single_task() -> DownloadResult:
        async with semaphore:
            return await download_once(session, url)

    results = list(
        await asyncio.gather(
            *(single_task() for _ in range(count)), return_exceptions=True
        )
    )
    for index, result in enumerate(results):
        if isinstance(result, Exception):
            results[index] = DownloadResult(0, 0, 0.0, False, repr(result))
    return results


def aimd_next(
    cwnd: int,
    wave_tp: float,
    ewma_prev: float,
    error_ratio: float,
    max_cwnd: int,
    best_cwnd: int,
) -> AimdStep:
    """Шаг AIMD с памятью оптимума.

    Деградация (ошибки или регресс пропускной способности > 10%) ->
    откат к последнему известному лучшему cwnd, иначе деление пополам.
    Прирост > 10% -> +1. Мёртвая зона +/-10% -> без изменений.
    """
    ewma = EWMA_ALPHA * wave_tp + (1 - EWMA_ALPHA) * ewma_prev

    is_degraded = error_ratio > MD_ERROR_RATIO or (
        ewma_prev > 0 and wave_tp < ewma_prev * DEGRADE_FACTOR
    )
    if is_degraded:
        recovered = best_cwnd if 0 < best_cwnd < cwnd else max(1, cwnd // MD_FACTOR)
        return AimdStep(recovered, ewma, False, True)

    is_improved = ewma_prev == 0 or wave_tp >= ewma_prev * AI_IMPROVEMENT_FACTOR
    if is_improved:
        return AimdStep(min(cwnd + 1, max_cwnd), ewma, True, False)

    return AimdStep(cwnd, ewma, False, False)


async def run_adaptive_benchmark(
    session: aiohttp.ClientSession,
    url: str,
    requests: int,
    start_cwnd: int,
    attempts: int,
    read_timeout: float,
    connect_timeout: float,
) -> BenchmarkOutcome:
    """Онлайн-AIMD замер: закачки одновременно и замер, и драйвер адаптации."""
    if requests < 1:
        raise ValueError("requests must be >= 1")

    results: list[DownloadResult] = []
    cwnd = max(1, start_cwnd)
    cwnd_max = cwnd
    remaining = requests
    next_request_id = 1
    ewma = 0.0
    best_cwnd = 0
    best_ewma = 0.0
    stable_min_request_id = 0
    wave_bytes: list[int] = []
    wave_wall_seconds: list[float] = []
    wave_start_request_ids: list[int] = []
    benchmark_started = time.perf_counter()

    while remaining > 0:
        wave_count = min(cwnd, remaining)
        wave = await run_wave(
            session,
            url,
            wave_count,
            wave_count,
            attempts,
            read_timeout,
            connect_timeout,
        )
        wave_start_request_ids.append(next_request_id)
        for result in wave:
            result.request_id = next_request_id
            next_request_id += 1
        results.extend(wave)

        successes = [result for result in wave if result.is_ok]
        wave_bytes.append(sum(result.bytes_downloaded for result in successes))
        # Агрегатный span волны = самая долгая параллельная закачка: суммировать
        # длительности параллельных стримов нельзя — это даёт скорость потока,
        # а не пропускную способность канала.
        wave_wall_seconds.append(
            max((result.duration_seconds for result in successes), default=0.0)
        )
        error_ratio = 1 - (len(successes) / len(wave))

        wave_tp = (
            wave_bytes[-1] / wave_wall_seconds[-1] if wave_wall_seconds[-1] > 0 else 0.0
        )
        step = aimd_next(
            cwnd, wave_tp, ewma, error_ratio, min(MAX_CONCURRENCY, requests), best_cwnd
        )
        if step.ewma > best_ewma and step.ewma > 0:
            best_ewma = step.ewma
            best_cwnd = cwnd

        logger.info(
            "волна cwnd=%s → %s (tp=%s Мбит/с, ewma=%s Мбит/с, ошибки=%s%%)%s",
            cwnd,
            step.new_cwnd,
            round(wave_tp * 8 / 1e6, 1),
            round(step.ewma * 8 / 1e6, 1),
            round(error_ratio * 100),
            " — откат к best" if step.is_degraded and best_cwnd < cwnd else "",
        )

        if stable_min_request_id == 0 and not step.is_improved and not step.is_degraded:
            stable_min_request_id = results[0].request_id

        cwnd = step.new_cwnd
        cwnd_max = max(cwnd_max, cwnd)
        ewma = step.ewma
        remaining -= wave_count

    if stable_min_request_id == 0:
        stable_min_request_id = results[0].request_id

    stable_bytes, stable_wall_seconds = _sum_stable_waves(
        wave_bytes, wave_wall_seconds, wave_start_request_ids, stable_min_request_id
    )
    return BenchmarkOutcome(
        results,
        stable_min_request_id,
        start_cwnd,
        cwnd_max,
        stable_bytes,
        stable_wall_seconds,
        time.perf_counter() - benchmark_started,
    )


def _sum_stable_waves(
    wave_bytes: Sequence[int],
    wave_wall_seconds: Sequence[float],
    wave_start_request_ids: Sequence[int],
    stable_min_request_id: int,
) -> tuple[int, float]:
    """Агрегат байт/время по волнам, начиная с первой стабильной."""
    total_bytes = 0
    total_wall = 0.0
    for index, wave_start in enumerate(wave_start_request_ids):
        if wave_start >= stable_min_request_id:
            total_bytes += wave_bytes[index]
            total_wall += wave_wall_seconds[index]
    return total_bytes, total_wall


def compute_mbps(total_bytes: int, elapsed_seconds: float) -> Decimal:
    """Биты/с точно (Fraction), затем перевод в мегабиты/с (1 Мбит = 1_000_000 бит)."""
    if elapsed_seconds <= 0 or total_bytes <= 0:
        return Decimal(0)
    bits_per_second = Fraction(total_bytes * 8, 1) / Fraction(elapsed_seconds)
    mbps = bits_per_second / Fraction(1_000_000, 1)
    return Decimal(mbps.numerator) / Decimal(mbps.denominator)

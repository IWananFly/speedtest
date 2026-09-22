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
    """Временная ошибка скачивания, которую стоит ретраить.

    Args:
        message: Текст ошибки.
        retry_after: Подсказка сервера (из ``Retry-After``) — сколько секунд
            подождать перед повтором; ``None``, если сервер не указал ожидание.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        """Инициализирует исключение и сохраняет подсказку времени повтора."""
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    """Возвращает секунды ожидания из заголовка ``Retry-After``.

    Поддерживает оба валидных формата: delta-seconds (``"120"``) и HTTP-date
    (``"Tue, 22 Sep 2026 12:00:00 GMT"``). Нераспознанное значение — ``None``.

    Args:
        value: Сырое значение заголовка или ``None``.

    Returns:
        Число секунд до повтора (не отрицательное), либо ``None``, если
        заголовок пуст или не разобрался.
    """
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
    """Считает паузу перед повтором для tenacity.

    Если исключение — ``RetryableDownloadError`` с заголовком ``Retry-After``,
    ждём указанное сервером время (в пределах [1, ``RETRY_WAIT_CAP_SECONDS``]).
    Иначе — экспоненциальный backoff с джиттером.

    Args:
        retry_state: Состояние повтора tenacity, откуда берётся последнее
            исключение.

    Returns:
        Секунды ожидания до следующей попытки.
    """
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
    """Результат одной попытки закачки (после всех ретраев).

    Attributes:
        request_id: Порядковый номер закачки в замере (1-based); 0 у фейлов,
            вернувшихся через ``retry_error_callback`` до присвоения номера.
        bytes_downloaded: Получено байт (0 у фейлов).
        duration_seconds: Время тела запроса (без ожиданий между попытками).
        is_ok: Успех.
        error: Текст ошибки или ``None``.
        retry_count: Число сделанных попыток (>= 1).
    """

    request_id: int
    bytes_downloaded: int
    duration_seconds: float
    is_ok: bool
    error: str | None
    retry_count: int = 0


@dataclass(frozen=True)
class AimdStep:
    """Один шаг адаптации AIMD.

    Attributes:
        new_cwnd: Параллельность следующей волны.
        ewma: Сглаженная пропускная способность после шага.
        is_improved: Признак «канал разгоняется» (AI).
        is_degraded: Признак деградации (ошибки или регресс скорости).
    """

    new_cwnd: int
    ewma: float
    is_improved: bool
    is_degraded: bool = False


@dataclass(frozen=True)
class WaveProgress:
    """Live-сводка одной завершившейся волны для колбэка прогресса.

    Attributes:
        wave_index: Номер волны (1-based) в порядке запуска.
        done_requests: Всего запросов завершено к моменту окончания волны.
        total_requests: Запланированное число закачек всего замера.
        ok_count: Успешных закачек в волне.
        fail_count: Неуспешных закачек в волне.
        cwnd: Параллельность этой волны.
        next_cwnd: Параллельность следующей волны (решение AIMD).
        wave_mbps: Агрегатная пропускная способность волны, Мбит/с.
        ewma_mbps: Сглаженная пропускная способность после шага, Мбит/с.
        is_improved: Признак разгона (AI).
        is_degraded: Признак деградации (ошибки или регресс tp).
    """

    wave_index: int
    done_requests: int
    total_requests: int
    ok_count: int
    fail_count: int
    cwnd: int
    next_cwnd: int
    wave_mbps: float
    ewma_mbps: float
    is_improved: bool
    is_degraded: bool


@dataclass(frozen=True)
class BenchmarkOutcome:
    """Итоги всего замера: все закачки и агрегаты стабильной фазы.

    Attributes:
        results: Все закачки в порядке запуска.
        stable_min_request_id: Первый ``request_id`` стабильной фазы.
        cwnd_start: Стартовая параллельность.
        cwnd_max: Максимальная достигнутая параллельность.
        stable_bytes: Сумма байт стабильных волн.
        stable_wall_seconds: Суммарный wall-clock span стабильных волн.
        total_wall_seconds: Полное время всего цикла замера.
    """

    results: Sequence[DownloadResult]
    stable_min_request_id: int
    cwnd_start: int
    cwnd_max: int
    stable_bytes: int
    stable_wall_seconds: float
    total_wall_seconds: float


def _describe_error(exc: BaseException) -> str:
    """Формирует человекопонятное описание исключения.

    Args:
        exc: Пойманное исключение.

    Returns:
        Строка вида ``"ТипОшибки: текст"``.
    """
    return f"{type(exc).__name__}: {exc}"


def _to_failed_result(retry_state: RetryCallState) -> DownloadResult:
    """Строит DownloadResult-фейл из исчерпанных ретраев (колбэк tenacity).

    Args:
        retry_state: Состояние повтора tenacity.

    Returns:
        Запись фейла с ``is_ok=False``, описанием последнего исключения и
        числом сделанных попыток.
    """
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
    """Скачивает тело URL целиком и возвращает результат одной попытки.

    Один проход без ретраев: на 4xx (не 408/429) — сразу ``DownloadResult``
    с ошибкой, на остальные временные статусы — ``RetryableDownloadError``.

    Args:
        session: Клиентская aiohttp-сессия.
        url: Адрес ресурса.
        read_timeout: Таймаут чтения тела.
        connect_timeout: Таймаут установки соединения.

    Returns:
        Результат успешной закачки или 4xx-отказа.

    Raises:
        RetryableDownloadError: На временный HTTP-статус (408/429/5xx) или
            короткое чтение (``size != Content-Length``).
    """
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
    if content_length is not None and size != content_length:  # pragma: no cover
        raise RetryableDownloadError(
            f"short read: got {size} of {content_length} bytes"
        )
    return DownloadResult(0, size, elapsed, True, None)


def make_download_once(
    attempts: int,
    read_timeout: float,
    connect_timeout: float,
) -> Callable[[aiohttp.ClientSession, str], Awaitable[DownloadResult]]:
    """Собирает функцию одной закачки с ретраями через tenacity.

    Обёртка ``download(session, url)`` ретраит все ``RETRYABLE_EXCEPTIONS``
    (временные HTTP-статусы, сетевые ошибки, таймауты, короткие чтения)
    максимум ``attempts`` раз с паузой по ``_wait_for_retry_after``.

    Args:
        attempts: Максимум попыток на одну закачку.
        read_timeout: Таймаут чтения тела.
        connect_timeout: Таймаут установки соединения.

    Returns:
        Функцию ``download(session, url) -> DownloadResult``.
    """

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
        logger.debug(
            "закачка: %s байт за %.3f с (%s, попыток %s)",
            result.bytes_downloaded,
            result.duration_seconds,
            "ok" if result.is_ok else result.error,
            result.retry_count,
        )
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
    """Запускает ``count`` параллельных закачек с ограничением ``concurrency``.

    Все закачки стартуют фактически одновременно (через ``asyncio.gather``);
    каждая считается своей отдельной волной если ``count == concurrency``.

    Args:
        session: Клиентская aiohttp-сессия.
        url: Адрес ресурса.
        concurrency: Лимит одновременных соединений (семафор).
        count: Сколько закачек запустить.
        attempts: Максимум попыток на каждую закачку.
        read_timeout: Таймаут чтения тела.
        connect_timeout: Таймаут установки соединения.

    Returns:
        Список результатов волны в порядке задач.
    """
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

    Считает сглаженную пропускную способность ``ewma`` и решает, куда двинуть
    параллельность. Деградация (ошибки ``error_ratio > MD_ERROR_RATIO`` или
    регресс скорости больше 10%) откатывает к последнему известному лучшему
    cwnd, иначе делит пополам. Прирост больше 10% — +1. Мёртвая зона ±10% —
    без изменений.

    Args:
        cwnd: Текущая параллельность волны.
        wave_tp: Агрегатная пропускная способность только что прошедшей волны.
        ewma_prev: Сглаженная пропускная способность за прошлые волны.
        error_ratio: Доля неуспешных закачек волны (0.0 — все успешны).
        max_cwnd: Верхний предел параллельности.
        best_cwnd: Лучшая параллельность из запомненного оптимума.

    Returns:
        Шаг со следующей параллельностью и обновлённым ``ewma``.
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
    on_wave: Callable[[WaveProgress], None] | None = None,
) -> BenchmarkOutcome:
    """Онлайн-AIMD замер: закачки одновременно и замер, и драйвер адаптации.

    Волнами. После каждой волны считаются агрегатные ``wave_tp`` (байты волны
    делить на wall-clock span — самую длинную параллельную закачку) и ошибки;
    ``aimd_next`` решает параллельность следующей волны. После остановки
    собираются агрегаты волн, начиная с первой «плато»-волны.

    Args:
        session: Клиентская aiohttp-сессия.
        url: Адрес ресурса.
        requests: Сколько закачек сделать всего.
        start_cwnd: Стартовая параллельность для AIMD.
        attempts: Максимум попыток на каждую закачку.
        read_timeout: Таймаут чтения тела.
        connect_timeout: Таймаут установки соединения.
        on_wave: Опциональный колбэк ``callback(wave)``, вызываемый после
            каждой волны со сводкой ``WaveProgress``; ``None`` — не вызывать.

    Returns:
        Итоги замера: все результаты и агрегаты стабильной фазы.

    Raises:
        ValueError: Если ``requests < 1``.
    """
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

        if on_wave is not None:
            on_wave(
                WaveProgress(
                    len(wave_start_request_ids),
                    next_request_id - 1,
                    requests,
                    len(successes),
                    len(wave) - len(successes),
                    cwnd,
                    step.new_cwnd,
                    wave_tp * 8 / 1e6,
                    step.ewma * 8 / 1e6,
                    step.is_improved,
                    step.is_degraded,
                )
            )

        logger.debug(
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
    """Агрегат байт/время по волнам, начиная с первой стабильной.

    Суммирует байты и wall-clock span только тех волн, чей первый закачившийся
    ``request_id`` не меньше ``stable_min_request_id``.

    Args:
        wave_bytes: Байты каждой волны (в порядке волн).
        wave_wall_seconds: Span каждой волны.
        wave_start_request_ids: ``request_id`` первого результата каждой волны.
        stable_min_request_id: Начало стабильной фазы.

    Returns:
        Кортеж ``(сумма байт, суммарный span)``.
    """
    total_bytes = 0
    total_wall = 0.0
    for index, wave_start in enumerate(wave_start_request_ids):
        if wave_start >= stable_min_request_id:
            total_bytes += wave_bytes[index]
            total_wall += wave_wall_seconds[index]
    return total_bytes, total_wall


def compute_mbps(total_bytes: int, elapsed_seconds: float) -> Decimal:
    """Переводит байты и время в мегабиты в секунду.

    Считает точно через ``Fraction``: ``байты*8 / время``, затем делит на
    1 000 000 бит (1 Мбит), результат возвращается как ``Decimal``.

    Args:
        total_bytes: Скачано байт.
        elapsed_seconds: Wall-clock время закачки.

    Returns:
        Десятичная скорость в Мбит/с; 0, если время или байты не положительны.
    """
    if elapsed_seconds <= 0 or total_bytes <= 0:
        return Decimal(0)
    bits_per_second = Fraction(total_bytes * 8, 1) / Fraction(elapsed_seconds)
    mbps = bits_per_second / Fraction(1_000_000, 1)
    return Decimal(mbps.numerator) / Decimal(mbps.denominator)

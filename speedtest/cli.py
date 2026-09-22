"""CLI-обвязка: аргументы, валидация, отчёт, запуск."""

import argparse
import asyncio
import contextlib
import logging
import sys

import aiohttp

from speedtest.core import (
    BenchmarkOutcome,
    compute_mbps,
    run_adaptive_benchmark,
)

DEFAULT_URL = "http://cachefly.cachefly.net/100mb.test"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
DEFAULT_REQUESTS = 10
DEFAULT_ATTEMPTS = 3
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_CONNECTOR_LIMIT = 32


def format_bytes(size: int) -> str:
    """Приводит размер в байтах к читаемому виду с кратными единицами.

    Args:
        size: Количество байт (>= 0).

    Returns:
        ``"X.XX GiB"``/``"X.XX MiB"``/``"X.XX KiB"`` в зависимости от кратности;
        значения < 1 KiB всё равно показываются как KiB.
    """
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.2f} MiB"
    return f"{size / 1024:.2f} KiB"


def render_report(outcome: BenchmarkOutcome) -> str:
    """Собирает текстовый отчёт о замере.

    Рисует таблицу закачек (объём, время, попытки, статус), сводки стабильной
    фазы: среднее время запроса, агрегат объёма, успехи/ошибки, параметры
    AIMD и итоговую агрегатную скорость в Мбит/с.

    Args:
        outcome: Итоги замера из ``run_adaptive_benchmark``.

    Returns:
        Многострочный отчёт для печати.
    """
    results = outcome.results
    stable = [
        result
        for result in results
        if result.request_id >= outcome.stable_min_request_id and result.is_ok
    ]
    successes = [result for result in results if result.is_ok]
    failures = [result for result in results if not result.is_ok]

    total_bytes = outcome.stable_bytes
    elapsed = outcome.stable_wall_seconds
    average_time = (
        sum(result.duration_seconds for result in stable) / len(stable)
        if stable
        else 0.0
    )
    speed = compute_mbps(total_bytes, elapsed)

    lines = ["", "--- результаты ---"]
    lines.append(
        f"{'запрос':>6} | {'объём':>10} | {'время, с':>9} | {'попытки':>7} | статус"
    )
    for result in results:
        status = "ok" if result.is_ok else f"fail: {result.error}"
        size = format_bytes(result.bytes_downloaded) if result.is_ok else "-"
        duration = f"{result.duration_seconds:.3f}" if result.is_ok else "-"
        lines.append(
            f"{result.request_id:>6} | {size:>10} | {duration:>9} "
            f"| {result.retry_count:>7} | {status}"
        )
    lines.append("-" * 58)
    lines.append(f"среднее время запроса (стабильная фаза): {average_time:.3f} с")
    lines.append(
        f"объём (стабильная фаза): {format_bytes(total_bytes)} ({total_bytes:,} байт)"
    )
    lines.append(f"успешно: {len(successes)} / {len(results)}")
    if failures:
        lines.append(f"ошибки: {len(failures)}")
    lines.append(f"параллельность: {outcome.cwnd_start} → {outcome.cwnd_max}")
    lines.append(f"стабильная фаза: с запроса #{outcome.stable_min_request_id}")
    lines.append(f"скорость: {speed:.2f} Мбит/с")
    lines.append(f"общее время выполнения: {outcome.total_wall_seconds:.3f} с")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки и валидирует их.

    Описание и флаги: URL (позиционный, опциональный), ``--requests/-n``,
    ``--concurrency/-c``, ``--attempts/-a``, ``--timeout``,
    ``--connect-timeout``, ``--verbose/-v``. При невалидных значениях вызывает
    ``parser.error`` (exit code 2).

    Returns:
        Namespace с параметрами: ``url, requests, concurrency, attempts,
        timeout, connect_timeout, verbose``.
    """
    parser = argparse.ArgumentParser(
        description="Асинхронный замер скорости интернета (параллельное "
        "скачивание ресурса)."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help="адрес ресурса (дефолт — тестовый файл 100 МБ на CacheFly CDN)",
    )
    parser.add_argument(
        "--requests",
        "-n",
        type=int,
        default=DEFAULT_REQUESTS,
        help="количество закачек (дефолт 10)",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=1,
        help="стартовая параллельность для AIMD, ≥ 1 (дефолт 1)",
    )
    parser.add_argument(
        "--attempts",
        "-a",
        type=int,
        default=DEFAULT_ATTEMPTS,
        help="максимум попыток на одну закачку (tenacity stop_after_attempt)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_READ_TIMEOUT,
        help="таймаут чтения тела, сек (дефолт 30)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help="таймаут установки соединения, сек (дефолт 10)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="подробный лог (включая ретраи)",
    )
    args = parser.parse_args()

    if args.requests < 1:
        parser.error("--requests должен быть >= 1")
    if args.attempts < 1:
        parser.error("--attempts должен быть >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency должен быть >= 1")
    if args.timeout <= 0 or args.connect_timeout <= 0:
        parser.error("таймауты должны быть положительными")
    return args


def _enable_utf8_stdio() -> None:
    """Переключает stdout/stderr на UTF-8 на Windows (если ещё не UTF-8).

    Ошибки перенастройки игнорируются: вывод просто остаётся в прежней кодировке.
    На не-Windows платформах ничего не делает.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding.lower() not in ("utf-8", "utf8"):
            with contextlib.suppress(AttributeError, ValueError, OSError):
                stream.reconfigure(encoding="utf-8")


async def main() -> int:
    """Полный прогон замера: аргументы, логика, печать отчёта.

    Открывает клиентскую aiohttp-сессию с лимитом соединений, гоняет
    ``run_adaptive_benchmark`` и печатает ``render_report``.

    Returns:
        0, если хотя бы одна закачка успешна, иначе 1.
    """
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    connector = aiohttp.TCPConnector(
        limit=max(DEFAULT_CONNECTOR_LIMIT, args.requests, args.concurrency)
    )
    async with aiohttp.ClientSession(
        connector=connector, headers={"User-Agent": DEFAULT_USER_AGENT}
    ) as session:
        outcome = await run_adaptive_benchmark(
            session,
            args.url,
            args.requests,
            args.concurrency,
            args.attempts,
            args.timeout,
            args.connect_timeout,
        )

    print(render_report(outcome))
    return 0 if any(result.is_ok for result in outcome.results) else 1


def run() -> None:
    """Точка входа ``python -m speedtest``: UTF-8 stdout и запуск ``main``.

    Raises:
        SystemExit: С кодом возврата ``main`` (0 — успех, 1 — все закачки упали).
    """
    _enable_utf8_stdio()
    raise SystemExit(asyncio.run(main()))

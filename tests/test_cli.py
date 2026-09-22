"""CLI: аргументы, отчёт, UTF-8 вывод и основной запуск."""

import asyncio
import logging
import sys
from argparse import Namespace
from io import StringIO

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import speedtest.cli as cli
from speedtest.core import BenchmarkOutcome, DownloadResult, WaveProgress

BODY = b"y" * 4096


def build_app() -> web.Application:
    """Приложение с маршрутами /ok и /perm для e2e-прогона main()."""
    app = web.Application()

    async def ok_handler(request: web.Request) -> web.Response:
        return web.Response(body=BODY)

    async def permanent_handler(request: web.Request) -> web.Response:
        return web.Response(status=400)

    app.router.add_get("/ok", ok_handler)
    app.router.add_get("/perm", permanent_handler)
    return app


def test_render_report_mixed_results():
    """Отчёт содержит таблицу, статусы, ошибки и агрегаты смешанного замера."""
    results = [
        DownloadResult(1, 2048, 1.0, True, None, 1),
        DownloadResult(2, 0, 0.0, False, "HTTP 400", 2),
        DownloadResult(3, 1024**2, 2.0, True, None, 1),
    ]
    outcome = BenchmarkOutcome(results, 1, 1, 4, 2048, 1.0, 3.5)
    report = cli.render_report(outcome)

    assert "--- результаты ---" in report
    assert "ok" in report
    assert "fail: HTTP 400" in report
    assert "1.00 MiB" in report
    assert "успешно: 2 / 3" in report
    assert "ошибки: 1" in report
    assert "параллельность: 1 → 4" in report
    assert "стабильная фаза: с запроса #1" in report
    assert "общее время выполнения: 3.500 с" in report
    assert "0.02 Мбит/с" in report


def test_render_report_without_stable_phase_or_failures():
    """Пустая стабильная фаза и отсутствие ошибок обрабатываются корректно."""
    results = [
        DownloadResult(1, 1024, 1.0, True, None, 1),
        DownloadResult(2, 1024, 1.0, True, None, 1),
    ]
    outcome = BenchmarkOutcome(results, 3, 1, 1, 2048, 2.0, 2.0)
    report = cli.render_report(outcome)

    assert "среднее время запроса (стабильная фаза): 0.000 с" in report
    assert "объём (стабильная фаза): 2.00 KiB (2,048 байт)" in report
    assert "ошибки" not in report


def test_render_report_empty_results():
    """Отчёт по пустому замеру не роняет отрисовку."""
    outcome = BenchmarkOutcome([], 1, 1, 1, 0, 0.0, 0.0)
    report = cli.render_report(outcome)

    assert "успешно: 0 / 0" in report
    assert "0.00 Мбит/с" in report


def test_parse_args_defaults(monkeypatch):
    """Без аргументов используются дефолтные значения."""
    monkeypatch.setattr(sys, "argv", ["speedtest"])

    args = cli.parse_args()

    assert args.url == cli.DEFAULT_URL
    assert args.requests == cli.DEFAULT_REQUESTS
    assert args.concurrency == 1
    assert args.attempts == cli.DEFAULT_ATTEMPTS
    assert args.timeout == cli.DEFAULT_READ_TIMEOUT
    assert args.connect_timeout == cli.DEFAULT_CONNECT_TIMEOUT
    assert args.verbose is False
    assert args.quiet is False


def test_parse_args_custom_values(monkeypatch):
    """Все флаги корректно разбираются в свои поля."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "speedtest",
            "http://example.test",
            "-n",
            "5",
            "-c",
            "2",
            "-a",
            "4",
            "--timeout",
            "3.5",
            "--connect-timeout",
            "2",
            "-q",
        ],
    )

    args = cli.parse_args()

    assert args.url == "http://example.test"
    assert args.requests == 5
    assert args.concurrency == 2
    assert args.attempts == 4
    assert args.timeout == 3.5
    assert args.connect_timeout == 2
    assert args.verbose is False
    assert args.quiet is True


def test_parse_args_rejects_quiet_with_verbose(monkeypatch):
    """Одновременные --verbose и --quiet несовместимы (exit code 2)."""
    monkeypatch.setattr(sys, "argv", ["speedtest", "-v", "-q"])

    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args()
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["-n", "0"],
        ["-a", "0"],
        ["-c", "0"],
        ["--timeout", "0"],
        ["--timeout", "-1"],
        ["--connect-timeout", "0"],
    ],
)
def test_parse_args_rejects_invalid_values(monkeypatch, argv):
    """Невалидные значения вызывают parser.error с кодом выхода 2."""
    monkeypatch.setattr(sys, "argv", ["speedtest", *argv])

    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args()
    assert exc_info.value.code == 2


class FakeStream:
    """Стендовая замена sys.stdout/stderr с счётчиком переконфигураций."""

    def __init__(self, encoding: str):
        """Устанавливает кодировку потока и обнуляет счётчик переконфигураций."""
        self.encoding = encoding
        self.reconfigure_calls = 0

    def reconfigure(self, **kwargs: object) -> None:
        """Имитирует переключение кодировки и увеличивает счётчик вызовов."""
        self.reconfigure_calls += 1


def test_enable_utf8_stdio_skips_non_windows(monkeypatch):
    """На не-Windows платформе ничего не переконфигурируется."""
    monkeypatch.setattr(sys, "platform", "linux")
    stream = FakeStream("cp1251")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", FakeStream("cp1251"))

    cli._enable_utf8_stdio()
    assert stream.reconfigure_calls == 0


def test_enable_utf8_stdio_reconfigures_windows(monkeypatch):
    """На Windows потоки не в UTF-8 переключаются на UTF-8."""
    monkeypatch.setattr(sys, "platform", "win32")
    out = FakeStream("cp1251")
    err = FakeStream("cp1251")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli._enable_utf8_stdio()
    assert out.reconfigure_calls == 1
    assert err.reconfigure_calls == 1


def test_enable_utf8_stdio_keeps_utf8_streams(monkeypatch):
    """Потоки уже в UTF-8 не трогаются."""
    monkeypatch.setattr(sys, "platform", "win32")
    out = FakeStream("utf-8")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", FakeStream("utf8"))

    cli._enable_utf8_stdio()
    assert out.reconfigure_calls == 0


def test_enable_utf8_stdio_ignores_reconfigure_errors(monkeypatch):
    """Сбой переконфигурации (нет метода) не роняет процесс."""

    class BrokenStream(FakeStream):
        def reconfigure(self, **kwargs: object) -> None:
            raise AttributeError("no reconfigure")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", BrokenStream("cp1251"))
    monkeypatch.setattr(sys, "stderr", BrokenStream("cp1251"))

    cli._enable_utf8_stdio()


class NonTtyStream(StringIO):
    """Поток stderr для тестов main(): неинтерактивный (isatty() = False)."""

    def isatty(self) -> bool:
        """Сообщает, что поток не подключён к терминалу."""
        return False


def run_main_with_server(
    monkeypatch, path: str, quiet: bool = False
) -> tuple[int, str]:
    """Гоняет cli.main() против локального сервера и возвращает (rc, stderr)."""
    stderr = NonTtyStream()
    monkeypatch.setattr(sys, "stderr", stderr)

    async def runner() -> int:
        server = TestServer(build_app())
        await server.start_server()
        try:
            url = str(server.make_url(path))
            monkeypatch.setattr(
                cli,
                "parse_args",
                lambda: Namespace(
                    url=url,
                    requests=2,
                    concurrency=1,
                    attempts=1,
                    timeout=5.0,
                    connect_timeout=5.0,
                    verbose=False,
                    quiet=quiet,
                ),
            )
            return await cli.main()
        finally:
            await server.close()

    return asyncio.run(runner()), stderr.getvalue()


def test_main_success_prints_report(monkeypatch, capsys):
    """Успешный прогон возвращает 0, печатает отчёт и прогресс волн."""
    rc, stderr = run_main_with_server(monkeypatch, "/ok")
    captured = capsys.readouterr().out

    assert rc == 0
    assert "--- результаты ---" in captured
    assert "успешно: 2 / 2" in captured
    assert "скорость:" in captured
    assert "волна 1:" in stderr
    assert "волна 2:" in stderr


def test_main_all_failed_returns_one(monkeypatch, capsys):
    """Подряд фейлы дают код возврата 1."""
    rc, _ = run_main_with_server(monkeypatch, "/perm")
    captured = capsys.readouterr().out

    assert rc == 1
    assert "успешно: 0 / 2" in captured


def test_main_quiet_hides_progress(monkeypatch, capsys):
    """Тихий режим убирает живые строки прогресса из stderr."""
    rc, stderr = run_main_with_server(monkeypatch, "/ok", quiet=True)
    captured = capsys.readouterr().out

    assert rc == 0
    assert "--- результаты ---" in captured
    assert "волна" not in captured
    assert "волна" not in stderr


def test_run_exits_with_main_code(monkeypatch):
    """run() пробрасывает код возврата main() через SystemExit."""
    monkeypatch.setattr(cli, "_enable_utf8_stdio", lambda: None)

    def fake_run(coroutine):
        coroutine.close()
        return 3

    monkeypatch.setattr(cli.asyncio, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        cli.run()
    assert exc_info.value.code == 3


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected_level"),
    [
        (True, False, logging.DEBUG),
        (False, True, logging.CRITICAL),
        (False, False, logging.INFO),
    ],
)
def test_log_level_selection(verbose, quiet, expected_level):
    """Уровень лога определяется флагами -v/-q: DEBUG / CRITICAL / INFO."""
    assert cli._log_level(verbose, quiet) == expected_level


def make_wave_progress(
    wave_index: int,
    ok_count: int,
    fail_count: int,
    done_requests: int = 3,
    *,
    wave_mbps: float = 512.3,
    cwnd: int = 4,
    next_cwnd: int = 5,
) -> WaveProgress:
    """Собирает типовую сводку волны для тестов прогресса."""
    return WaveProgress(
        wave_index,
        done_requests,
        10,
        ok_count,
        fail_count,
        cwnd,
        next_cwnd,
        wave_mbps,
        480.0,
        False,
        False,
    )


def test_progress_line_all_ok():
    """Формат строки прогресса при всех успехах."""
    progress = make_wave_progress(3, 3, 0)
    assert cli._progress_line(progress) == "волна 3: 3/3 ok · 512.3 Мбит/с · cwnd 4→5"


def test_progress_line_with_failures():
    """При ошибках статус перечисляет успешные и упавшие закачки."""
    progress = make_wave_progress(2, 2, 1)
    assert (
        cli._progress_line(progress)
        == "волна 2: 2 ok, 1 ошиб · 512.3 Мбит/с · cwnd 4→5"
    )


def test_make_progress_writer_disabled_returns_none():
    """Выключенный прогресс возвращает None (без писателя)."""
    assert cli.make_progress_writer(StringIO(), False, True) is None


def test_make_progress_writer_writes_lines_when_not_live():
    """Неинтерактивный поток получает строки с переводом; finish ничего не делает."""
    stream = StringIO()
    writer = cli.make_progress_writer(stream, True, False)

    writer.write(make_wave_progress(1, 3, 0))
    writer.write(make_wave_progress(2, 2, 1))
    writer.finish()

    assert stream.getvalue() == (
        "волна 1: 3/3 ok · 512.3 Мбит/с · cwnd 4→5\n"
        "волна 2: 2 ok, 1 ошиб · 512.3 Мбит/с · cwnd 4→5\n"
    )


def test_make_progress_writer_overwrites_in_place_when_live():
    """Интерактивный поток пишет строку на месте и finish добавляет перевод."""
    stream = StringIO()
    writer = cli.make_progress_writer(stream, True, True)

    writer.write(make_wave_progress(1, 3, 0))
    writer.finish()

    value = stream.getvalue()
    assert value.startswith("\rволна 1: 3/3 ok · 512.3 Мбит/с · cwnd 4→5 ")
    assert value.endswith("\n")

"""CLI: аргументы, отчёт, UTF-8 вывод и основной запуск."""

import asyncio
import sys
from argparse import Namespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import speedtest.cli as cli
from speedtest.core import BenchmarkOutcome, DownloadResult

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
            "-v",
        ],
    )

    args = cli.parse_args()

    assert args.url == "http://example.test"
    assert args.requests == 5
    assert args.concurrency == 2
    assert args.attempts == 4
    assert args.timeout == 3.5
    assert args.connect_timeout == 2
    assert args.verbose is True


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


def run_main_with_server(monkeypatch, path: str) -> int:
    """Гоняет cli.main() против локального сервера по заданному маршруту."""

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
                ),
            )
            return await cli.main()
        finally:
            await server.close()

    return asyncio.run(runner())


def test_main_success_prints_report(monkeypatch, capsys):
    """Успешный прогон возвращает 0 и печатает отчёт."""
    rc = run_main_with_server(monkeypatch, "/ok")
    captured = capsys.readouterr().out

    assert rc == 0
    assert "--- результаты ---" in captured
    assert "успешно: 2 / 2" in captured
    assert "скорость:" in captured


def test_main_all_failed_returns_one(monkeypatch, capsys):
    """Подряд фейлы дают код возврата 1."""
    rc = run_main_with_server(monkeypatch, "/perm")
    captured = capsys.readouterr().out

    assert rc == 1
    assert "успешно: 0 / 2" in captured


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

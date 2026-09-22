# AGENTS.md — конвенции проекта speedtest

Асинхронный CLI-спидтест на Python (aiohttp + tenacity), пакет `speedtest/`,
тесты в `tests/`. Управление окружением — только через **uv**.

## Запуск и проверки (обязательны перед сдачей работы)

```console
uv run ruff format speedtest tests
uv run ruff check speedtest tests
uv run pytest -q
uv run pytest --cov=speedtest --cov-report=term-missing   # держим 100%
```

Живой смоук (уходит в интернет): `uv run speedtest -n 4 -a 1`.

## Структура

- `speedtest/core.py` — вся логика: скачивание, AIMD, метрики. **Без вывода
  в консоль** (прогресс — через callback `on_wave`, логи — через `logging`).
- `speedtest/cli.py` — argparse, валидация, стартовая строка, live-прогресс
  и отчёт. Прогресс и стартовая строка → **stderr**; итоговый отчёт →
  **stdout**.
- `tests/conftest.py` — общий локальный `aiohttp.TestServer`
  (фикстуры `run_with_server`, `app`, `body`). Тесты **не ходят в интернет**.

## Стиль кода

- Функции вместо классов; RORO (принял объект — вернул объект); чистые
  функции там, где нет I/O; `def` для синхронного, `async def` для I/O.
- Тип-хинты на все сигнатуры; Pydantic не нужен (нет моделей ввода).
- Guard clauses и ранние return вместо вложенных if; без лишних else.
- Описательные имена с вспомогательными глаголами/прилагательными
  (`is_ok`, `is_improved`, `has_*`).
- Файлы/директории — lowercase_with_underscores.

## Документация

- Все публичные функции и классы — Google-стиль докстрингов (ruff rule `D`).
- Версию держать синхронно: `pyproject.toml` `[project].version` и
  `speedtest/__init__.py.__version__`.

## Линтеры

ruff: `select = ["E", "F", "B", "I", "UP", "D", "SIM", "C4", "RUF", "PT"]`,
`ignore = ["D401", "RUF001", "RUF002", "RUF003"]`, pydocstyle convention
`google`, line-length 88.

## Язык

- Пользовательские строки CLI и докстринги — на **русском**.
- Для русских существительных после числа — хелпер
  `cli._plural(n, (форма1, форма2_5, форма5+))`, не «1 закачек».
- Прогресс/отчёт-примеры в README держать свежими: перегенерировать живым
  прогоном, не переписывать руками на глаз.

## Зависимости

Только `aiohttp` и `tenacity` в `[project]`; dev: `pytest`, `ruff`,
`coverage`, `pytest-cov`. Новые зависимости не добавлять без явной
необходимости; `requirements.txt` не использовать (есть `uv.lock`).

## Git

- Коммитить **только** файлы `speedtest/`; соседние проекты монорепо
  (`2d_game/`, `beautify_json/`, `horror_game/`), `.opencode/`, `__pycache__`
  не трогать.
- Пуш отдельного репозитория — через subtree:
  `git subtree split --prefix=speedtest -b speedtest-latest` из корня
  монорепо, затем `git push <url> speedtest-latest:main`, затем удалить
  ветку.

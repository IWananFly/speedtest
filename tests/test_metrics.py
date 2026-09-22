"""Расчёты скорости и форматирование объёма."""

from decimal import Decimal

from speedtest.cli import format_bytes
from speedtest.core import compute_mbps


def test_zero_bytes_on_positive_elapsed():
    """Ноль байт при положительном времени даёт скорость 0."""
    assert compute_mbps(0, 5) == Decimal(0)


def test_zero_elapsed_returns_zero():
    """Нулевое время при положительных байтах даёт скорость 0."""
    assert compute_mbps(1000, 0) == Decimal(0)


def test_exact_50_mb_over_one_second():
    """50 МБ за секунду — ровно 400 Мбит/с (1 Мбит = 1 000 000 бит)."""
    assert compute_mbps(50_000_000, 1) == Decimal(400)


def test_one_mb_over_ten_seconds():
    """1 МБ за 10 секунд даёт 0.8 Мбит/с."""
    assert compute_mbps(1_000_000, 10) == Decimal("0.8")


def test_fractional_throughput_exact():
    """Дробные скорости считаются точно, без плавающих мегабит."""
    assert compute_mbps(1, 3) == Decimal(8) / Decimal(3_000_000)


def test_half_second_doubles_rate():
    """Вдвое меньшее время — вдвое большая скорость."""
    assert compute_mbps(50_000_000, 0.5) == Decimal(800)


def test_zero_bytes():
    """Ноль байт форматируется как 0.00 KiB."""
    assert format_bytes(0) == "0.00 KiB"


def test_kib_gib_boundaries():
    """Границы кратности: 1024^1/2/3 дают KiB/MiB/GiB."""
    assert format_bytes(1024) == "1.00 KiB"
    assert format_bytes(1024**2) == "1.00 MiB"
    assert format_bytes(1024**3) == "1.00 GiB"


def test_rounding_inside_unit():
    """Внутри единицы значение округляется до двух знаков."""
    assert format_bytes(1536) == "1.50 KiB"

"""Кейсы шага AIMD: AI, MD, откат к лучшему cwnd, мёртвая зона."""

from speedtest.core import aimd_next


def test_first_wave_always_improves():
    step = aimd_next(
        cwnd=1, wave_tp=100.0, ewma_prev=0.0, error_ratio=0.0, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 2
    assert step.ewma == 50.0
    assert step.is_improved is True
    assert step.is_degraded is False


def test_improvement_of_ten_percent_increments():
    step = aimd_next(
        cwnd=3, wave_tp=120.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 4
    assert step.ewma == 110.0
    assert step.is_improved is True


def test_improvement_capped_by_max_cwnd():
    step = aimd_next(
        cwnd=6, wave_tp=200.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 6
    assert step.is_improved is True


def test_dead_zone_keeps_cwnd():
    step = aimd_next(
        cwnd=3, wave_tp=105.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 3
    assert step.is_improved is False
    assert step.is_degraded is False


def test_speed_regression_triggers_degradation_and_uses_best_cwnd():
    step = aimd_next(
        cwnd=4, wave_tp=80.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=2
    )
    assert step.new_cwnd == 2
    assert step.is_degraded is True


def test_degradation_falls_back_to_half_without_best_cwnd():
    step = aimd_next(
        cwnd=4, wave_tp=80.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 2
    assert step.is_degraded is True


def test_high_error_ratio_triggers_degradation_even_with_high_speed():
    step = aimd_next(
        cwnd=3, wave_tp=150.0, ewma_prev=100.0, error_ratio=0.5, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 1
    assert step.is_degraded is True


def test_fallback_floor_is_one():
    step = aimd_next(
        cwnd=1, wave_tp=80.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=0
    )
    assert step.new_cwnd == 1
    assert step.is_degraded is True


def test_best_cwnd_equals_current_uses_fallback():
    step = aimd_next(
        cwnd=2, wave_tp=80.0, ewma_prev=100.0, error_ratio=0.0, max_cwnd=6, best_cwnd=2
    )
    assert step.new_cwnd == 1
    assert step.is_degraded is True


def test_ewma_smooths_throughput():
    step = aimd_next(
        cwnd=2, wave_tp=50.0, ewma_prev=30.0, error_ratio=0.1, max_cwnd=6, best_cwnd=0
    )
    assert step.ewma == 40.0
    assert step.is_improved is True

"""Асинхронный замер скорости интернета с онлайн-подбором параллельности (AIMD)."""

from speedtest.core import (
    AimdStep,
    BenchmarkOutcome,
    DownloadResult,
    RetryableDownloadError,
    aimd_next,
    compute_mbps,
    run_adaptive_benchmark,
)

__version__ = "1.5.0"

__all__ = [
    "AimdStep",
    "BenchmarkOutcome",
    "DownloadResult",
    "RetryableDownloadError",
    "aimd_next",
    "compute_mbps",
    "run_adaptive_benchmark",
]

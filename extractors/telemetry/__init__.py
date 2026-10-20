"""Telemetry package with a Platform Abstraction Layer (lazy, `sys.platform`-based backend selection)."""
from __future__ import annotations

import sys

from extractors.telemetry.base import BaseTelemetrySampler, RawReading, TelemetrySample

__all__ = [
    "BaseTelemetrySampler",
    "RawReading",
    "TelemetrySample",
    "TelemetrySampler",
    "get_sampler_class",
]


def get_sampler_class(platform: str | None = None) -> type[BaseTelemetrySampler]:
    plat = platform or sys.platform
    if plat.startswith("linux"):
        from extractors.telemetry.linux_telemetry import LinuxTelemetry

        return LinuxTelemetry
    from extractors.telemetry.windows_telemetry import WindowsTelemetry  # psutil: Windows and other platforms

    return WindowsTelemetry


class TelemetrySampler(BaseTelemetrySampler):
    """Factory-style facade: `TelemetrySampler()` returns the backend for the running OS."""

    def __new__(cls, *args, **kwargs):  # type: ignore[no-untyped-def]
        if cls is TelemetrySampler:
            return get_sampler_class()(*args, **kwargs)
        return super().__new__(cls)

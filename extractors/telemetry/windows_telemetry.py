"""Windows (and generic psutil) telemetry backend."""
from __future__ import annotations

import psutil

from extractors.telemetry.base import BaseTelemetrySampler, RawReading


class WindowsTelemetry(BaseTelemetrySampler):
    def __init__(self, publish_interval: float = 15.0, swing: float = 15.0) -> None:
        super().__init__(publish_interval, swing)
        psutil.cpu_percent(interval=None)

    def read_raw(self) -> RawReading:
        memory = psutil.virtual_memory()
        return RawReading(
            cpu_percent=psutil.cpu_percent(interval=None),
            ram_used=int(memory.used),
            ram_total=int(memory.total),
        )

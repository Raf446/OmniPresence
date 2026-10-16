"""Hardware telemetry sampled with psutil."""
from __future__ import annotations

import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class TelemetrySample:
    cpu_percent: float
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    published_cpu: int
    published_ram: int

    def as_variables(self) -> dict[str, str]:
        return {"cpu": str(self.published_cpu), "ram": str(self.published_ram)}


class TelemetrySampler:
    """Smooths CPU/RAM readings and republishes them sparingly.

    Presence text built from telemetry would otherwise change every second and burn
    Discord's update allowance, so the published values only move on a large swing
    or after `publish_interval` seconds.
    """

    def __init__(self, publish_interval: float = 30.0, swing: float = 15.0) -> None:
        self._publish_interval = publish_interval
        self._swing = swing
        self._cpu_avg: float | None = None
        self._published_at = 0.0
        self._published_cpu = 0
        self._published_ram = 0
        psutil.cpu_percent(interval=None)

    def sample(self) -> TelemetrySample:
        now = time.monotonic()
        cpu = psutil.cpu_percent(interval=None)
        self._cpu_avg = cpu if self._cpu_avg is None else 0.3 * cpu + 0.7 * self._cpu_avg
        memory = psutil.virtual_memory()

        first = self._published_at == 0.0
        stale = now - self._published_at >= self._publish_interval
        moved = (
            abs(self._cpu_avg - self._published_cpu) >= self._swing
            or abs(memory.percent - self._published_ram) >= self._swing
        )
        if first or stale or moved:
            self._published_cpu = int(round(self._cpu_avg))
            self._published_ram = int(round(memory.percent))
            self._published_at = now

        gib = 1024**3
        return TelemetrySample(
            cpu_percent=cpu,
            ram_percent=memory.percent,
            ram_used_gb=memory.used / gib,
            ram_total_gb=memory.total / gib,
            published_cpu=self._published_cpu,
            published_ram=self._published_ram,
        )

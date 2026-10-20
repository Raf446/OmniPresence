"""Telemetry interface shared by all platforms: sample dataclass plus the publish-smoothing logic."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TelemetrySample:
    cpu_percent: float
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float
    published_cpu: int
    published_ram: int

    def as_variables(self) -> dict[str, str]:
        return {
            "cpu": str(self.published_cpu),
            "ram": str(self.published_ram),
            "cpu_percent": str(self.published_cpu),
            "ram_used_gb": f"{self.ram_used_gb:.1f}",
            "ram_total_gb": f"{self.ram_total_gb:.0f}",
        }


@dataclass(frozen=True)
class RawReading:
    """Instantaneous OS reading. RAM values are in bytes."""

    cpu_percent: float
    ram_used: int
    ram_total: int

    @property
    def ram_percent(self) -> float:
        return (self.ram_used / self.ram_total * 100.0) if self.ram_total > 0 else 0.0


class BaseTelemetrySampler:
    """Smooths CPU/RAM readings and republishes them sparingly.

    Presence text built from telemetry would otherwise change every second and burn
    Discord's update allowance, so the published values only move on a large swing
    or after `publish_interval` seconds. Backends only implement `read_raw()`.
    """

    def __init__(self, publish_interval: float = 15.0, swing: float = 15.0) -> None:
        self._publish_interval = publish_interval
        self._swing = swing
        self._cpu_avg: float | None = None
        self._published_at = 0.0
        self._published_cpu = 0
        self._published_ram = 0

    def read_raw(self) -> RawReading:
        raise NotImplementedError

    def sample(self) -> TelemetrySample:
        now = time.monotonic()
        raw = self.read_raw()
        cpu = raw.cpu_percent
        ram_percent = raw.ram_percent
        self._cpu_avg = cpu if self._cpu_avg is None else 0.3 * cpu + 0.7 * self._cpu_avg

        first = self._published_at == 0.0
        stale = now - self._published_at >= self._publish_interval
        moved = (
            abs(self._cpu_avg - self._published_cpu) >= self._swing
            or abs(ram_percent - self._published_ram) >= self._swing
        )
        if first or stale or moved:
            self._published_cpu = int(round(self._cpu_avg))
            self._published_ram = int(round(ram_percent))
            self._published_at = now

        gib = 1024**3
        return TelemetrySample(
            cpu_percent=cpu,
            ram_percent=ram_percent,
            ram_used_gb=raw.ram_used / gib,
            ram_total_gb=raw.ram_total / gib,
            published_cpu=self._published_cpu,
            published_ram=self._published_ram,
        )

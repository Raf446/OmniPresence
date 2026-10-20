"""Linux telemetry backend: zero-dependency parsing of /proc/stat (CPU delta) and /proc/meminfo (RAM)."""
from __future__ import annotations

from extractors.telemetry.base import BaseTelemetrySampler, RawReading

PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"


def read_proc_file(path: str) -> str:
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        return handle.read()


def parse_proc_stat(text: str) -> tuple[int, int]:
    """Return (total_jiffies, idle_jiffies) from the aggregate 'cpu' line."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            fields = [int(value) for value in parts[1:]]
            fields += [0] * (8 - len(fields))
            user, nice, system, idle, iowait, irq, softirq, steal = fields[:8]
            return user + nice + system + idle + iowait + irq + softirq + steal, idle + iowait
    raise ValueError("no aggregate 'cpu' line in /proc/stat")


def parse_meminfo(text: str) -> tuple[int, int]:
    """Return (used_bytes, total_bytes). Used = MemTotal - MemAvailable (kernel >= 3.14 estimate)."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key.strip()] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1)
    total = values.get("MemTotal", 0)
    if total <= 0:
        raise ValueError("MemTotal missing from /proc/meminfo")
    available = values.get("MemAvailable")
    if available is None:
        available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
    return max(total - available, 0), total


class LinuxTelemetry(BaseTelemetrySampler):
    def __init__(self, publish_interval: float = 15.0, swing: float = 15.0) -> None:
        super().__init__(publish_interval, swing)
        self._prev: tuple[int, int] | None = None
        try:
            self._prev = parse_proc_stat(read_proc_file(PROC_STAT))
        except (OSError, ValueError):
            self._prev = None

    def _cpu_percent(self) -> float:
        total, idle = parse_proc_stat(read_proc_file(PROC_STAT))
        prev, self._prev = self._prev, (total, idle)
        if prev is None:
            return 0.0
        d_total, d_idle = total - prev[0], idle - prev[1]
        if d_total <= 0:
            return 0.0
        return min(max((1.0 - d_idle / d_total) * 100.0, 0.0), 100.0)

    def read_raw(self) -> RawReading:
        used, total = parse_meminfo(read_proc_file(PROC_MEMINFO))
        return RawReading(cpu_percent=self._cpu_percent(), ram_used=used, ram_total=total)

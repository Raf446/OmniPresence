"""Lightweight Daily Activity & Category Analytics Engine for OmniPresence v2.3.0.

Tracks accumulated time across 5 core categories (Coding, Media, Gaming, Browsing, Idle)
and recent session timeline entries, persisting to data/analytics.json.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from core.priority_engine import Resolution, Tier

CATEGORIES: tuple[str, ...] = ("Coding", "Media", "Gaming", "Browsing", "Idle")

CATEGORY_COLORS: dict[str, str] = {
    "Coding": "#38BDF8",    # Cyan Accent
    "Media": "#10B981",     # Emerald Green
    "Gaming": "#A855F7",    # Purple Neon
    "Browsing": "#F59E0B",  # Amber Gold
    "Idle": "#64748B",      # Slate Muted
}

CATEGORY_ICONS: dict[str, str] = {
    "Coding": "\U0001f4bb",
    "Media": "\U0001f3b5",
    "Gaming": "\U0001f3ae",
    "Browsing": "\U0001f310",
    "Idle": "\U0001f319",
}


def format_analytics_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def classify_resolution_category(resolution: Resolution | None) -> tuple[str, str, str]:
    """Return (category, headline, rule_id) for a given Resolution."""
    if resolution is None or resolution.rule is None or resolution.presence is None:
        return "Idle", "System Standby", "idle-standby"

    rule = resolution.rule
    presence = resolution.presence
    rule_id = rule.id.lower()
    headline = (presence.details or rule.name or rule.id).strip()
    if presence.state:
        headline = f"{headline} \u2022 {presence.state.strip()}"

    if rule.tier == Tier.IDLE or rule.kind in ("afk", "standby"):
        return "Idle", headline, rule.id
    if rule.tier == Tier.GAME:
        return "Gaming", headline, rule.id
    if rule.tier == Tier.MEDIA or rule_id == "browser-youtube":
        return "Media", headline, rule.id
    if "browser" in rule_id or any(b in rule_id for b in ("chrome", "brave", "edge", "firefox", "web")):
        return "Browsing", headline, rule.id
    if rule.tier in (Tier.BUILD, Tier.EDITOR, Tier.MANUAL):
        return "Coding", headline, rule.id
    return "Coding", headline, rule.id


@dataclass
class SessionRecord:
    category: str
    headline: str
    rule_id: str
    started_at: float
    updated_at: float
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "headline": self.headline,
            "rule_id": self.rule_id,
            "started_at": round(self.started_at, 2),
            "updated_at": round(self.updated_at, 2),
            "duration_seconds": round(self.duration_seconds, 2),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SessionRecord":
        return cls(
            category=str(raw.get("category", "Idle")),
            headline=str(raw.get("headline", "Activity")),
            rule_id=str(raw.get("rule_id", "")),
            started_at=float(raw.get("started_at", time.time())),
            updated_at=float(raw.get("updated_at", time.time())),
            duration_seconds=float(raw.get("duration_seconds", 0.0)),
        )


@dataclass
class DailyAnalyticsSnapshot:
    date_str: str
    totals: dict[str, float] = field(default_factory=lambda: {c: 0.0 for c in CATEGORIES})
    sessions: list[SessionRecord] = field(default_factory=list)
    active_category: str = "Idle"
    active_headline: str = "System Standby"

    @property
    def total_tracked_seconds(self) -> float:
        return sum(max(0.0, float(v)) for v in self.totals.values())

    @property
    def total_active_seconds(self) -> float:
        return sum(max(0.0, float(v)) for k, v in self.totals.items() if k != "Idle")

    def percentages(self) -> dict[str, float]:
        denom = self.total_tracked_seconds
        if denom <= 0.0:
            return {c: 0.0 for c in CATEGORIES}
        return {c: (max(0.0, float(self.totals.get(c, 0.0))) / denom) * 100.0 for c in CATEGORIES}


class AnalyticsManager(QObject):
    """Thread-safe daily time tracker persisting category totals and recent sessions to data/analytics.json."""

    analytics_updated = Signal(object)  # DailyAnalyticsSnapshot

    _instance: "AnalyticsManager | None" = None
    _lock = threading.Lock()
    SAVE_INTERVAL_SECONDS = 15.0
    MAX_SESSIONS = 25

    def __init__(self, path: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._data_Lock = threading.Lock()
        default_path = path or (Path(__file__).resolve().parent.parent / "data" / "analytics.json")
        self._path = default_path
        self._date_str = datetime.now().strftime("%Y-%m-%d")
        self._totals: dict[str, float] = {c: 0.0 for c in CATEGORIES}
        self._sessions: list[SessionRecord] = []
        self._history_days: dict[str, Any] = {}

        self._active_category = "Idle"
        self._active_headline = "System Standby"
        self._active_rule_id = "idle-standby"
        self._last_tick_mono = time.monotonic()
        self._last_save_mono = 0.0

        self._load()

    @classmethod
    def instance(cls, path: Path | None = None) -> "AnalyticsManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(path=path)
            return cls._instance

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._data_Lock:
            self._date_str = today
            self._totals = {c: 0.0 for c in CATEGORIES}
            self._sessions = []
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raw = {}
            except (OSError, ValueError):
                raw = {}

            days = raw.get("days")
            if isinstance(days, dict):
                self._history_days = days
                day_entry = days.get(today)
                if isinstance(day_entry, dict):
                    raw_totals = day_entry.get("totals") or {}
                    for cat in CATEGORIES:
                        try:
                            self._totals[cat] = max(0.0, float(raw_totals.get(cat, 0.0)))
                        except (TypeError, ValueError):
                            self._totals[cat] = 0.0
                    raw_sessions = day_entry.get("sessions") or []
                    if isinstance(raw_sessions, list):
                        for item in raw_sessions[: self.MAX_SESSIONS]:
                            if isinstance(item, dict):
                                self._sessions.append(SessionRecord.from_dict(item))
            elif raw.get("date") == today and isinstance(raw.get("totals"), dict):
                for cat in CATEGORIES:
                    try:
                        self._totals[cat] = max(0.0, float(raw["totals"].get(cat, 0.0)))
                    except (TypeError, ValueError):
                        self._totals[cat] = 0.0

    def save(self) -> None:
        with self._data_Lock:
            payload_day = {
                "totals": {c: round(self._totals.get(c, 0.0), 2) for c in CATEGORIES},
                "total_active_seconds": round(
                    sum(v for k, v in self._totals.items() if k != "Idle"), 2
                ),
                "total_tracked_seconds": round(sum(self._totals.values()), 2),
                "sessions": [s.to_dict() for s in self._sessions[: self.MAX_SESSIONS]],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._history_days[self._date_str] = payload_day
            doc = {
                "date": self._date_str,
                "totals": payload_day["totals"],
                "sessions": payload_day["sessions"],
                "days": self._history_days,
            }
            text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(text, encoding="utf-8")
                self._last_save_mono = time.monotonic()
            except OSError:
                pass

    def _rollover_day_if_needed(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date_str:
            self._date_str = today
            self._totals = {c: 0.0 for c in CATEGORIES}
            self._sessions = []

    def record_tick(
        self,
        resolution: Resolution | None,
        *,
        elapsed_override: float | None = None,
        force_save: bool = False,
    ) -> DailyAnalyticsSnapshot:
        """Accumulate elapsed time since last tick into the active category and update session timeline."""
        now_mono = time.monotonic()
        now_wall = time.time()
        cat, headline, rule_id = classify_resolution_category(resolution)

        with self._data_Lock:
            self._rollover_day_if_needed()
            if elapsed_override is not None:
                dt = max(0.0, float(elapsed_override))
            else:
                dt = max(0.0, min(now_mono - self._last_tick_mono, 30.0))
            self._last_tick_mono = now_mono

            if dt > 0.0:
                self._totals[cat] = self._totals.get(cat, 0.0) + dt

            # Update or create top session record
            if (
                self._sessions
                and self._sessions[0].category == cat
                and self._sessions[0].rule_id == rule_id
            ):
                top = self._sessions[0]
                top.headline = headline
                top.updated_at = now_wall
                top.duration_seconds += dt
            else:
                new_rec = SessionRecord(
                    category=cat,
                    headline=headline,
                    rule_id=rule_id,
                    started_at=now_wall,
                    updated_at=now_wall,
                    duration_seconds=max(dt, 1.0),
                )
                self._sessions.insert(0, new_rec)
                if len(self._sessions) > self.MAX_SESSIONS:
                    self._sessions = self._sessions[: self.MAX_SESSIONS]

            self._active_category = cat
            self._active_headline = headline
            self._active_rule_id = rule_id

            should_save = force_save or (now_mono - self._last_save_mono >= self.SAVE_INTERVAL_SECONDS)

        if should_save:
            self.save()

        snap = self.snapshot()
        try:
            self.analytics_updated.emit(snap)
        except RuntimeError:
            pass
        return snap

    def reset_today(self) -> DailyAnalyticsSnapshot:
        """Reset today's accumulated category totals and session history."""
        with self._data_Lock:
            self._date_str = datetime.now().strftime("%Y-%m-%d")
            self._totals = {c: 0.0 for c in CATEGORIES}
            self._sessions = []
            self._last_tick_mono = time.monotonic()
        self.save()
        snap = self.snapshot()
        try:
            self.analytics_updated.emit(snap)
        except RuntimeError:
            pass
        return snap

    def snapshot(self) -> DailyAnalyticsSnapshot:
        with self._data_Lock:
            self._rollover_day_if_needed()
            return DailyAnalyticsSnapshot(
                date_str=self._date_str,
                totals=dict(self._totals),
                sessions=list(self._sessions),
                active_category=self._active_category,
                active_headline=self._active_headline,
            )


ANALYTICS_MANAGER = AnalyticsManager.instance()

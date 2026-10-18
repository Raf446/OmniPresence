"""Rule loading, template rendering and priority arbitration.

This module is Qt-free so the decision logic can be exercised without a GUI.
Tier order (lower number wins): Manual > Build > Game > Editor > Media > Idle.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from core.settings_manager import SETTINGS_MANAGER
from extractors.celestial_sync import CELESTIAL_SYNC
from extractors.github_resolver import (
    DEFAULT_GITHUB_USER,
    GITHUB_AVATAR_RESOLVER,
    extract_github_owner,
)
from extractors.mood_engine import MOOD_ENGINE
from extractors.youtube_resolver import YOUTUBE_THUMBNAIL_RESOLVER




class RuleError(ValueError):
    """Raised when rules.json cannot be turned into a valid RuleBook."""


class Tier(IntEnum):
    MANUAL = 1
    BUILD = 2
    GAME = 3
    EDITOR = 4
    MEDIA = 5
    IDLE = 6

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> "Tier":
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            names = ", ".join(t.label for t in cls)
            raise RuleError(f"unknown tier {value!r} (expected one of: {names})") from None


ACTIVITY_TYPES = {"playing": 0, "listening": 2, "watching": 3, "competing": 5}
WORKSPACE_STRATEGIES = frozenset({"vscode_storage", "cmdline", "cwd", "build_cwd"})
TIMESTAMP_MODES = frozenset({"session", "process", "media", "none"})
DISCORD_TEXT_LIMIT = 128
DISCORD_LABEL_LIMIT = 32
DISCORD_URL_LIMIT = 512

_TOKEN = re.compile(r"\[\[(.*?)\]\]|\{([A-Za-z_][A-Za-z0-9_]*(?:\|[^{}]*)?)\}", re.S)
_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\|[^{}]*)?)\}")
_OPTIONAL = re.compile(r"\[\[.*?\]\]", re.S)
_SPACES = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_LEADING_TITLE_NOISE = re.compile(
    r"^(?:(?:\(\d+\)|\[\d+\])\s*|(?:[▶►▸▹⏵⏸⏹⏺🔊🔉🔈🔇🔴🟢🎵🎶🎧📺]\ufe0f?|\u25b6\ufe0f?|\u23f8\ufe0f?)\s*)+"
)
_BROWSER_MEDIA_TOKENS = ("chrome", "brave", "msedge", "edge", "firefox", "opera", "vivaldi")


def sanitize_window_title(title: str) -> str:
    """Strip zero-width Unicode chars, leading notification counts, and play/audio glyphs."""
    if not title:
        return ""
    cleaned = _ZERO_WIDTH.sub("", title)
    cleaned = _LEADING_TITLE_NOISE.sub("", cleaned)
    return _SPACES.sub(" ", cleaned).strip()


def _lookup(name: str, variables: Mapping[str, str]) -> str:
    if "|" in name:
        primary, fallback = name.split("|", 1)
        resolved = _lookup(primary.strip(), variables)
        return resolved if resolved else fallback.strip()
    # A "_q" suffix yields the URL-encoded form of the base variable.
    if name.endswith("_q"):
        base = variables.get(name[:-2], "")
        return quote(base, safe="") if base else ""
    return variables.get(name, "")



def render(template: str, variables: Mapping[str, str]) -> str | None:
    """Fill a template. Returns None if a required variable is empty.

    Text inside [[...]] is dropped as a whole when any variable in it is empty.
    """
    required = _VAR.findall(_OPTIONAL.sub("", template))
    if any(not _lookup(name, variables) for name in required):
        return None

    def substitute(match: re.Match[str]) -> str:
        inner = match.group(1)
        if inner is None:
            return _lookup(match.group(2), variables)
        if any(not _lookup(name, variables) for name in _VAR.findall(inner)):
            return ""
        return _VAR.sub(lambda m: _lookup(m.group(1), variables), inner)

    text = _SPACES.sub(" ", _TOKEN.sub(substitute, template)).strip()
    return text or None


def render_first(templates: Sequence[str], variables: Mapping[str, str]) -> str | None:
    for template in templates:
        text = render(template, variables)
        if text:
            return text
    return None


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


def _discord_text(value: str | None) -> str | None:
    if value is None or len(value) < 2:
        return None
    return value if len(value) <= DISCORD_TEXT_LIMIT else value[: DISCORD_TEXT_LIMIT - 1] + "\u2026"


def sanitize_buttons(pairs: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Validate and sanitize Discord Rich Presence buttons (max 2, non-empty label, strict https:// URL)."""
    valid: list[tuple[str, str]] = []
    for raw_label, raw_url in pairs:
        label = (raw_label or "").strip()[:DISCORD_LABEL_LIMIT]
        url = (raw_url or "").strip()
        if not label or not url:
            continue
        if not url.lower().startswith("https://") or len(url) <= len("https://a.b") or " " in url:
            continue
        if len(url) > DISCORD_URL_LIMIT:
            continue
        valid.append((label, url))
        if len(valid) >= 2:
            break
    return tuple(valid)


@dataclass(frozen=True)
class CustomRPCConfig:
    """Full PreMiD-style Custom Rich Presence configuration bound to P1 Manual Override."""

    details: str = ""
    state: str = ""
    large_image: str = ""
    large_text: str = ""
    small_image: str = ""
    small_text: str = ""
    button1_label: str = ""
    button1_url: str = ""
    button2_label: str = ""
    button2_url: str = ""
    timestamp_mode: str = "elapsed"  # "none", "elapsed", or "custom"
    duration_minutes: int = 60
    activity_type: str = "playing"
    app_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "details": self.details,
            "state": self.state,
            "large_image": self.large_image,
            "large_text": self.large_text,
            "small_image": self.small_image,
            "small_text": self.small_text,
            "button1_label": self.button1_label,
            "button1_url": self.button1_url,
            "button2_label": self.button2_label,
            "button2_url": self.button2_url,
            "timestamp_mode": self.timestamp_mode,
            "duration_minutes": int(self.duration_minutes),
            "activity_type": self.activity_type,
            "app_id": self.app_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CustomRPCConfig":
        ts_mode = str(data.get("timestamp_mode", "elapsed")).strip().lower()
        if ts_mode not in ("none", "elapsed", "custom"):
            ts_mode = "elapsed"
        act_type = str(data.get("activity_type", "playing")).strip().lower()
        if act_type not in ACTIVITY_TYPES:
            act_type = "playing"
        try:
            dur = max(1, min(int(data.get("duration_minutes", 60)), 1440))
        except (TypeError, ValueError):
            dur = 60
        return cls(
            details=str(data.get("details", "")).strip(),
            state=str(data.get("state", "")).strip(),
            large_image=str(data.get("large_image", "")).strip(),
            large_text=str(data.get("large_text", "")).strip(),
            small_image=str(data.get("small_image", "")).strip(),
            small_text=str(data.get("small_text", "")).strip(),
            button1_label=str(data.get("button1_label", "")).strip(),
            button1_url=str(data.get("button1_url", "")).strip(),
            button2_label=str(data.get("button2_label", "")).strip(),
            button2_url=str(data.get("button2_url", "")).strip(),
            timestamp_mode=ts_mode,
            duration_minutes=dur,
            activity_type=act_type,
            app_id=str(data.get("app_id", "")).strip(),
        )


@dataclass(frozen=True)
class WindowFacts:
    pid: int
    process: str
    title: str
    fullscreen: bool
    started_at: float | None


@dataclass(frozen=True)
class Activity:
    """One detected context, ready to be rendered by its rule."""

    rule_id: str
    key: str
    variables: Mapping[str, str]
    started_at: float | None = None
    ends_at: float | None = None
    foreground: bool = True


@dataclass(frozen=True)
class PresenceData:
    details: str | None
    state: str | None
    large_image: str | None
    large_text: str | None
    small_image: str | None
    small_text: str | None
    start: int | None
    end: int | None
    buttons: tuple[tuple[str, str], ...]
    activity_type: int
    tier: int = 6
    rule_id: str = ""

    def to_kwargs(self) -> dict[str, Any]:
        fields = {
            "details": self.details,
            "state": self.state,
            "large_image": self.large_image,
            "large_text": self.large_text,
            "small_image": self.small_image,
            "small_text": self.small_text,
            "start": self.start,
            "end": self.end,
        }
        kwargs = {key: value for key, value in fields.items() if value is not None}
        clean_btns = sanitize_buttons(self.buttons)
        if clean_btns:
            kwargs["buttons"] = [{"label": label, "url": url} for label, url in clean_btns]
        return kwargs



@dataclass(frozen=True)
class PresenceSpec:
    details: tuple[str, ...]
    state: tuple[str, ...]
    large_image: tuple[str, ...]
    large_text: tuple[str, ...]
    small_image: tuple[str, ...]
    small_text: tuple[str, ...]
    timestamp: str
    activity_type: int


@dataclass(frozen=True)
class ButtonSpec:
    label: str
    url: str


@dataclass(frozen=True)
class MatchSpec:
    processes: frozenset[str] = frozenset()
    exclude_processes: frozenset[str] = frozenset()
    title_patterns: tuple[re.Pattern[str], ...] = ()
    fullscreen: bool = False
    builds: bool = False
    media_apps: tuple[str, ...] | None = None
    manual: bool = False
    afk: bool = False
    standby: bool = False


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    enabled: bool
    tier: Tier
    match: MatchSpec
    presence: PresenceSpec
    buttons: tuple[ButtonSpec, ...]
    workspace: tuple[str, ...]
    sticky_seconds: float
    idle_exempt: bool
    session_key: str

    @property
    def kind(self) -> str:
        m = self.match
        if m.manual:
            return "manual"
        if m.afk:
            return "afk"
        if m.standby:
            return "standby"
        if m.builds:
            return "build"
        if m.media_apps is not None:
            return "media"
        return "window"

    @property
    def summary(self) -> str:
        m = self.match
        kind = self.kind
        if kind == "manual":
            return "Set from the action bar"
        if kind == "afk":
            return "No input past the idle threshold"
        if kind == "standby":
            return "Nothing else detected"
        if kind == "build":
            return "Compiler and build processes"
        if kind == "media":
            if not m.media_apps:
                return "Any media session"
            return "Media session: " + ", ".join(m.media_apps)
        parts: list[str] = []
        if m.processes:
            names = sorted(m.processes)
            shown = ", ".join(names[:3])
            parts.append(shown + (f" +{len(names) - 3}" if len(names) > 3 else ""))
        if m.fullscreen:
            parts.append("fullscreen window")
        if m.title_patterns:
            parts.append(f"{len(m.title_patterns)} title pattern" + ("s" if len(m.title_patterns) > 1 else ""))
        return ", ".join(parts)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _parse_rule(raw: Mapping[str, Any], position: int) -> Rule:
    where = f"rules[{position}]"
    rule_id = str(raw.get("id", "")).strip()
    if not rule_id:
        raise RuleError(f"{where}: missing 'id'")
    where = f"rule '{rule_id}'"
    tier = Tier.parse(raw.get("tier", ""))

    raw_match = raw.get("match") or {}
    if not isinstance(raw_match, Mapping):
        raise RuleError(f"{where}: 'match' must be an object")
    patterns: list[re.Pattern[str]] = []
    for pattern in _as_list(raw_match.get("title_regex")):
        try:
            patterns.append(re.compile(pattern))
        except re.error as exc:
            raise RuleError(f"{where}: invalid title_regex {pattern!r}: {exc}") from None
    media = raw_match.get("media")
    media_apps = None
    if media is not None:
        media_apps = tuple(app.lower() for app in _as_list(media.get("apps")))
    match = MatchSpec(
        processes=frozenset(p.lower() for p in _as_list(raw_match.get("processes"))),
        exclude_processes=frozenset(p.lower() for p in _as_list(raw_match.get("exclude_processes"))),
        title_patterns=tuple(patterns),
        fullscreen=bool(raw_match.get("fullscreen", False)),
        builds=bool(raw_match.get("builds", False)),
        media_apps=media_apps,
        manual=bool(raw_match.get("manual", False)),
        afk=bool(raw_match.get("afk", False)),
        standby=bool(raw_match.get("standby", False)),
    )
    is_window_rule = bool(match.processes or match.title_patterns or match.fullscreen)
    flagged = [match.builds, match.media_apps is not None, match.manual, match.afk, match.standby]
    if sum(flagged) + (1 if is_window_rule else 0) != 1:
        raise RuleError(f"{where}: 'match' must describe exactly one kind of trigger")

    raw_presence = raw.get("presence")
    if not isinstance(raw_presence, Mapping) or not raw_presence.get("details"):
        raise RuleError(f"{where}: 'presence.details' is required")
    timestamp = str(raw_presence.get("timestamp", "none"))
    if timestamp not in TIMESTAMP_MODES:
        raise RuleError(f"{where}: timestamp must be one of {sorted(TIMESTAMP_MODES)}")
    activity_name = str(raw_presence.get("activity_type", "playing")).lower()
    if activity_name not in ACTIVITY_TYPES:
        raise RuleError(f"{where}: activity_type must be one of {sorted(ACTIVITY_TYPES)}")
    presence = PresenceSpec(
        details=tuple(_as_list(raw_presence.get("details"))),
        state=tuple(_as_list(raw_presence.get("state"))),
        large_image=tuple(_as_list(raw_presence.get("large_image"))),
        large_text=tuple(_as_list(raw_presence.get("large_text"))),
        small_image=tuple(_as_list(raw_presence.get("small_image"))),
        small_text=tuple(_as_list(raw_presence.get("small_text"))),
        timestamp=timestamp,
        activity_type=ACTIVITY_TYPES[activity_name],
    )

    buttons = []
    for item in raw.get("buttons") or []:
        label, url = str(item.get("label", "")).strip(), str(item.get("url", "")).strip()
        if not label or not url:
            raise RuleError(f"{where}: every button needs 'label' and 'url'")
        buttons.append(ButtonSpec(label, url))
    if len(buttons) > 2:
        raise RuleError(f"{where}: Discord allows at most two buttons")

    workspace = tuple(_as_list(raw.get("workspace")))
    unknown = [item for item in workspace if item not in WORKSPACE_STRATEGIES]
    if unknown:
        raise RuleError(f"{where}: unknown workspace strategy {unknown[0]!r}")

    return Rule(
        id=rule_id,
        name=str(raw.get("name", rule_id)),
        enabled=bool(raw.get("enabled", True)),
        tier=tier,
        match=match,
        presence=presence,
        buttons=tuple(buttons),
        workspace=workspace,
        sticky_seconds=float(raw.get("sticky_seconds", 0)),
        idle_exempt=bool(raw.get("idle_exempt", False)),
        session_key=str(raw.get("session_key", "")),
    )


class RuleBook:
    """Immutable, validated view of rules.json."""

    def __init__(
        self,
        rules: Sequence[Rule],
        process_labels: Mapping[str, str] | None = None,
        languages: Mapping[str, str] | None = None,
    ) -> None:
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.process_labels = {k.lower(): v for k, v in (process_labels or {}).items()}
        self.languages = {k.lower(): v for k, v in (languages or {}).items()}
        self._index = {rule.id: position for position, rule in enumerate(self.rules)}

    @classmethod
    def load(cls, path: Path) -> "RuleBook":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuleError(f"{path.name} not found") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuleError(f"{path.name} is not valid JSON: {exc}") from None
        if not isinstance(raw, Mapping) or not isinstance(raw.get("rules"), list):
            raise RuleError(f"{path.name} must contain a 'rules' array")
        rules = [_parse_rule(item, position) for position, item in enumerate(raw["rules"])]
        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                raise RuleError(f"duplicate rule id '{rule.id}'")
            seen.add(rule.id)
        return cls(rules, raw.get("process_labels"), raw.get("languages"))

    @staticmethod
    def persist_enabled(path: Path, rule_id: str, enabled: bool) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw.get("rules", []):
            if item.get("id") == rule_id:
                item["enabled"] = enabled
                break
        else:
            raise RuleError(f"rule '{rule_id}' not found in {path.name}")
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def get(self, rule_id: str) -> Rule | None:
        position = self._index.get(rule_id)
        return None if position is None else self.rules[position]

    def order_of(self, rule_id: str) -> int:
        return self._index.get(rule_id, len(self.rules))

    def of_kind(self, kind: str) -> list[Rule]:
        return [rule for rule in self.rules if rule.enabled and rule.kind == kind]

    def with_enabled(self, rule_id: str, enabled: bool) -> "RuleBook":
        updated = [replace(r, enabled=enabled) if r.id == rule_id else r for r in self.rules]
        return RuleBook(updated, self.process_labels, self.languages)

    def label_for(self, process: str) -> str:
        key = process.lower()
        if key in self.process_labels:
            return self.process_labels[key]
        stem = key[:-4] if key.endswith(".exe") else key
        return stem.replace("_", " ").replace("-", " ").title()

    def media_label(self, app_id: str) -> str:
        lowered = app_id.lower()
        for process, label in self.process_labels.items():
            stem = process[:-4] if process.endswith(".exe") else process
            if stem and stem in lowered:
                return label
        base = re.split(r"[!_]", app_id)[0]
        base = base[:-4] if base.lower().endswith(".exe") else base
        return base.split(".")[-1] or app_id

    def language_for(self, filename: str) -> str:
        suffix = Path(filename.strip(" *\u25cf\u2022")).suffix.lower()
        return self.languages.get(suffix, "")

    def match_window(
        self, window: WindowFacts | None, build_active: bool
    ) -> list[tuple[Rule, dict[str, str]]]:
        """Rules triggered by the foreground window or by a running build."""
        hits: list[tuple[Rule, dict[str, str]]] = []
        clean_title = sanitize_window_title(window.title) if window is not None else ""
        for rule in self.rules:
            if not rule.enabled:
                continue
            kind = rule.kind
            if kind == "build":
                if build_active:
                    hits.append((rule, {}))
                continue
            if kind != "window" or window is None:
                continue
            m = rule.match
            if m.processes and window.process not in m.processes:
                continue
            if window.process in m.exclude_processes:
                continue
            if m.fullscreen and not window.fullscreen:
                continue
            groups: dict[str, str] = {}
            if m.title_patterns:
                for pattern in m.title_patterns:
                    found = pattern.search(clean_title)
                    if not found and clean_title != window.title:
                        found = pattern.search(window.title)
                    if found:
                        groups = {
                            k: (v or "").strip(" -–—|•\t")
                            for k, v in found.groupdict().items()
                        }
                        break
                else:
                    continue
            if rule.id == "browser-youtube" and groups.get("video"):
                groups["youtube_thumbnail"] = YOUTUBE_THUMBNAIL_RESOLVER.resolve(
                    groups["video"], background=True
                )
            elif rule.id == "browser-github":
                display_name = groups.get("display_name", "").strip()
                raw_owner = (
                    f"{display_name} ({groups['owner']})"
                    if (display_name and groups.get("owner"))
                    else (
                        groups.get("owner")
                        or groups.get("owner_only")
                        or groups.get("owner_alt")
                        or ""
                    )
                ).strip()
                owner = extract_github_owner(raw_owner) if raw_owner else ""
                repo = groups.get("repo", "").strip()
                section = groups.get("section", "").strip()
                if owner and repo:
                    groups["owner"] = owner
                    groups["details"] = repo
                    groups["state"] = f"by @{owner}"
                    groups["github_avatar"] = GITHUB_AVATAR_RESOLVER.resolve(owner, background=True)
                    groups["github_url"] = f"https://github.com/{owner}/{repo}"
                elif owner and not section:
                    groups["owner"] = owner
                    groups["details"] = "Viewing Profile"
                    groups["state"] = f"@{owner}"
                    groups["github_avatar"] = GITHUB_AVATAR_RESOLVER.resolve(owner, background=True)
                    groups["github_url"] = f"https://github.com/{owner}"
                else:
                    groups.pop("owner", None)
                    groups["details"] = f"Browsing {section or 'GitHub'}"
                    groups["state"] = "Working on GitHub"
                    groups["github_avatar"] = (
                        GITHUB_AVATAR_RESOLVER.resolve(DEFAULT_GITHUB_USER, background=True)
                        or f"https://github.com/{DEFAULT_GITHUB_USER}.png?size=512"
                    )
                    groups["github_url"] = "https://github.com"
            hits.append((rule, groups))
        return hits

    def match_media(self, app_id: str) -> Rule | None:
        lowered = app_id.lower()
        for rule in self.of_kind("media"):
            apps = rule.match.media_apps or ()
            if not apps or any(app in lowered for app in apps):
                return rule
        return None


@dataclass(frozen=True)
class EngineInput:
    now: float
    activities: Sequence[Activity]
    media: Any
    manual: CustomRPCConfig | tuple[str, str] | None
    paused: bool
    idle_seconds: float
    shared_variables: Mapping[str, str]


@dataclass(frozen=True)
class Resolution:
    presence: PresenceData | None
    tier: Tier | None
    rule: Rule | None
    activity: Activity | None
    live_tiers: frozenset[Tier]
    live_rule_ids: frozenset[str]
    sent: bool
    reason: str

    @property
    def trigger(self) -> tuple[str, str] | None:
        if self.rule is None or self.activity is None:
            return None
        return (self.rule.id, self.activity.key)


class PriorityEngine:
    SESSION_GRACE = 120.0

    def __init__(
        self,
        rulebook: RuleBook,
        idle_threshold: float,
        show_standby: bool = True,
        show_buttons: bool = True,
        mood_engine_enabled: bool = True,
        mood_apply_to_small_badge: bool = True,
        celestial_sync_enabled: bool = True,
        celestial_append_to_state: bool = True,
        privacy_show_city: bool | None = None,
        idle_custom_image: str | None = None,
    ) -> None:
        self.rulebook = rulebook
        self.idle_threshold = idle_threshold
        self.show_standby = show_standby
        self.show_buttons = show_buttons
        self.mood_engine_enabled = mood_engine_enabled
        self.mood_apply_to_small_badge = mood_apply_to_small_badge
        self.celestial_sync_enabled = celestial_sync_enabled
        self.celestial_append_to_state = celestial_append_to_state
        self.privacy_show_city = privacy_show_city
        self.idle_custom_image = idle_custom_image
        self._session_start: dict[str, float] = {}
        self._session_seen: dict[str, float] = {}

    def configure(
        self,
        idle_threshold: float,
        show_standby: bool,
        show_buttons: bool,
        *,
        mood_engine_enabled: bool | None = None,
        mood_apply_to_small_badge: bool | None = None,
        celestial_sync_enabled: bool | None = None,
        celestial_append_to_state: bool | None = None,
        privacy_show_city: bool | None = None,
        idle_custom_image: str | None = None,
    ) -> None:
        self.idle_threshold = idle_threshold
        self.show_standby = show_standby
        self.show_buttons = show_buttons
        if mood_engine_enabled is not None:
            self.mood_engine_enabled = bool(mood_engine_enabled)
        if mood_apply_to_small_badge is not None:
            self.mood_apply_to_small_badge = bool(mood_apply_to_small_badge)
        if celestial_sync_enabled is not None:
            self.celestial_sync_enabled = bool(celestial_sync_enabled)
        if celestial_append_to_state is not None:
            self.celestial_append_to_state = bool(celestial_append_to_state)
        if privacy_show_city is not None:
            self.privacy_show_city = bool(privacy_show_city)
        if idle_custom_image is not None:
            self.idle_custom_image = str(idle_custom_image)

    def resolve(self, inp: EngineInput) -> Resolution:
        book = self.rulebook
        shared = dict(inp.shared_variables)
        candidates = self._collect(inp, book, shared)

        afk = inp.idle_seconds >= self.idle_threshold
        if afk:
            candidates = [
                (rule, act)
                for rule, act in candidates
                if rule.idle_exempt or rule.tier not in (Tier.EDITOR, Tier.GAME)
            ]
            for rule in book.of_kind("afk"):
                variables = {**shared, "idle_for": format_duration(inp.idle_seconds)}
                candidates.append((rule, Activity(rule.id, rule.id, variables)))
                break
        elif not candidates and self.show_standby:
            for rule in book.of_kind("standby"):
                candidates.append((rule, Activity(rule.id, rule.id, dict(shared))))
                break

        live_tiers = frozenset(rule.tier for rule, _ in candidates)
        live_rules = frozenset(rule.id for rule, _ in candidates)
        if not candidates:
            return Resolution(None, None, None, None, live_tiers, live_rules, False, "nothing to show")

        # Active/foreground contexts (foreground=True, including live P1/P2/P3/P4/P5) always beat
        # background sticky carry-overs (foreground=False), preventing sticky IDE rules from blocking Spotify/media.
        rule, activity = min(
            candidates,
            key=lambda pair: (0 if pair[1].foreground else 1, pair[0].tier, book.order_of(pair[0].id)),
        )
        self._touch_sessions(candidates, inp.now)
        presence = self._render(rule, activity, shared, inp.now, inp.manual)

        outranked = len(candidates) - 1
        reason = f"P{int(rule.tier)} {rule.tier.label} via '{rule.id}'"
        if outranked:
            reason += f", outranks {outranked} other live candidate" + ("s" if outranked > 1 else "")
        if inp.paused:
            return Resolution(presence, rule.tier, rule, activity, live_tiers, live_rules, False, "paused: " + reason)
        return Resolution(presence, rule.tier, rule, activity, live_tiers, live_rules, True, reason)

    @staticmethod
    def _coerce_manual(manual: CustomRPCConfig | tuple[str, str] | None) -> CustomRPCConfig | None:
        if manual is None:
            return None
        if isinstance(manual, CustomRPCConfig):
            return manual
        details, state = manual
        return CustomRPCConfig(details=details, state=state)

    def _collect(
        self, inp: EngineInput, book: RuleBook, shared: Mapping[str, str]
    ) -> list[tuple[Rule, Activity]]:
        media = inp.media
        browser_media_active = bool(
            media is not None
            and any(tok in media.app_id.lower() for tok in _BROWSER_MEDIA_TOKENS)
        )

        found: list[tuple[Rule, Activity]] = []
        for activity in inp.activities:
            rule = book.get(activity.rule_id)
            if rule is not None and rule.enabled:
                if (
                    not activity.foreground
                    and rule.id == "browser-youtube"
                    and browser_media_active
                ):
                    activity = replace(activity, foreground=True)
                found.append((rule, activity))

        if media is not None:
            rule = book.match_media(media.app_id)
            if rule is not None:
                raw_art = (getattr(media, "artwork_url", "") or "").strip()
                valid_art = (
                    raw_art
                    if raw_art.lower().startswith("https://") and len(raw_art) <= 256
                    else ""
                )
                variables = {
                    **shared,
                    "track": media.title,
                    "artist": media.artist,
                    "album": media.album,
                    "artwork_url": valid_art,
                    "app_label": book.media_label(media.app_id),
                    "query": f"{media.title} {media.artist}".strip(),
                }
                key = render(rule.session_key, variables) if rule.session_key else None
                found.append(
                    (rule, Activity(rule.id, key or media.title, variables, media.start_at, media.end_at))
                )

        manual_cfg = self._coerce_manual(inp.manual)
        if manual_cfg is not None:
            for rule in book.of_kind("manual"):
                variables = {
                    **shared,
                    "manual_details": manual_cfg.details,
                    "manual_state": manual_cfg.state,
                }
                session_key = f"{rule.id}:{manual_cfg.timestamp_mode}:{manual_cfg.duration_minutes}"
                found.append((rule, Activity(rule.id, session_key, variables)))
                break
        return found

    def _touch_sessions(self, candidates: Sequence[tuple[Rule, Activity]], now: float) -> None:
        for rule, activity in candidates:
            skey = f"{rule.id}:{activity.key}"
            self._session_start.setdefault(skey, now)
            self._session_seen[skey] = now
        for skey in [k for k, seen in self._session_seen.items() if now - seen > self.SESSION_GRACE]:
            self._session_seen.pop(skey, None)
            self._session_start.pop(skey, None)

    def _timestamps(self, rule: Rule, activity: Activity) -> tuple[int | None, int | None]:
        mode = rule.presence.timestamp
        if mode == "none":
            return None, None
        session = self._session_start.get(f"{rule.id}:{activity.key}")
        if mode == "session":
            return (int(session) if session else None), None
        if mode == "process" and activity.started_at:
            return int(activity.started_at), None
        if mode == "media" and activity.started_at:
            end = int(activity.ends_at) if activity.ends_at else None
            return int(activity.started_at), end
        return (int(session) if session else None), None

    def _render_custom_manual(
        self, rule: Rule, activity: Activity, cfg: CustomRPCConfig, now: float
    ) -> PresenceData:
        session = self._session_start.get(f"{rule.id}:{activity.key}", now)
        if cfg.timestamp_mode == "none":
            start, end = None, None
        elif cfg.timestamp_mode == "custom":
            start = int(session)
            end = int(session + max(1, int(cfg.duration_minutes)) * 60)
        else:
            start, end = int(session), None

        buttons: tuple[tuple[str, str], ...] = ()
        if self.show_buttons:
            buttons = sanitize_buttons(
                (
                    (cfg.button1_label, cfg.button1_url),
                    (cfg.button2_label, cfg.button2_url),
                )
            )

        large_image = cfg.large_image.strip() or "manual"
        large_text = _discord_text(cfg.large_text.strip() or cfg.details.strip() or "Custom Rich Presence")
        small_image = cfg.small_image.strip() or ("manual" if not cfg.large_image.strip() else None)
        small_text = _discord_text(
            cfg.small_text.strip() or ("Set from OmniPresence" if small_image == "manual" else None)
        )
        act_code = ACTIVITY_TYPES.get(cfg.activity_type.lower(), rule.presence.activity_type)

        return PresenceData(
            details=_discord_text(cfg.details.strip() or "Custom Rich Presence"),
            state=_discord_text(cfg.state.strip()) if cfg.state.strip() else None,
            large_image=large_image,
            large_text=large_text,
            small_image=small_image,
            small_text=small_text,
            start=start,
            end=end,
            buttons=buttons,
            activity_type=act_code,
            tier=int(rule.tier),
            rule_id=rule.id,
        )

    def _render(
        self,
        rule: Rule,
        activity: Activity,
        shared: Mapping[str, str],
        now: float,
        manual: CustomRPCConfig | tuple[str, str] | None = None,
    ) -> PresenceData:
        if rule.kind == "manual":
            manual_cfg = self._coerce_manual(manual)
            if manual_cfg is not None:
                return self._render_custom_manual(rule, activity, manual_cfg, now)

        variables = {**shared, **activity.variables}
        if not variables.get("cpu_percent") or not variables.get("ram_used_gb") or not variables.get("ram_total_gb"):
            try:
                import psutil

                mem = psutil.virtual_memory()
                gib = 1024**3
                cpu_now = int(round(psutil.cpu_percent(interval=None)))
                variables.setdefault("cpu", str(cpu_now))
                variables.setdefault("cpu_percent", str(cpu_now))
                variables.setdefault("ram", str(int(round(mem.percent))))
                variables.setdefault("ram_used_gb", f"{mem.used / gib:.1f}")
                variables.setdefault("ram_total_gb", f"{mem.total / gib:.0f}")
            except Exception:
                variables.setdefault("cpu_percent", variables.get("cpu", "8"))
                variables.setdefault("ram_used_gb", "6.4")
                variables.setdefault("ram_total_gb", "16")

        if self.celestial_sync_enabled and CELESTIAL_SYNC.enabled:
            snap = CELESTIAL_SYNC.snapshot()
            for k, v in snap.as_variables().items():
                variables.setdefault(k, v)
        else:
            for k in (
                "ambient_vibe",
                "temp_c",
                "weather_desc",
                "city_name",
                "celestial_icon",
                "solar_phase",
                "lunar_phase",
            ):
                variables.pop(k, None)

        if rule.id == "browser-youtube":
            video_title = variables.get("video", "").strip()
            if video_title:
                variables["youtube_thumbnail"] = YOUTUBE_THUMBNAIL_RESOLVER.resolve(
                    video_title, background=True
                )
            else:
                variables.setdefault("youtube_thumbnail", "youtube")
        elif rule.id == "browser-github":
            display_name = variables.get("display_name", "").strip()
            raw_owner = (
                f"{display_name} ({variables['owner']})"
                if (display_name and variables.get("owner"))
                else (
                    variables.get("owner")
                    or variables.get("owner_only")
                    or variables.get("owner_alt")
                    or ""
                )
            ).strip()
            owner = extract_github_owner(raw_owner) if raw_owner else ""
            repo = variables.get("repo", "").strip()
            section = variables.get("section", "").strip()
            if owner and repo:
                variables["owner"] = owner
                variables["details"] = repo
                variables["state"] = f"by @{owner}"
                variables["github_avatar"] = GITHUB_AVATAR_RESOLVER.resolve(
                    owner, background=True
                )
                variables["github_url"] = f"https://github.com/{owner}/{repo}"
            elif owner and not section:
                variables["owner"] = owner
                variables["details"] = "Viewing Profile"
                variables["state"] = f"@{owner}"
                variables["github_avatar"] = GITHUB_AVATAR_RESOLVER.resolve(
                    owner, background=True
                )
                variables["github_url"] = f"https://github.com/{owner}"
            else:
                variables.pop("owner", None)
                variables["details"] = f"Browsing {section or 'GitHub'}"
                variables["state"] = "Working on GitHub"
                variables["github_avatar"] = (
                    GITHUB_AVATAR_RESOLVER.resolve(DEFAULT_GITHUB_USER, background=True)
                    or f"https://github.com/{DEFAULT_GITHUB_USER}.png?size=512"
                )
                variables["github_url"] = "https://github.com"

        # Adaptive Mood Engine: quantize cover/thumbnail URL into {mood_badge} and {mood_name}
        mood_source_url = (
            variables.get("artwork_url")
            or variables.get("youtube_thumbnail")
            or ""
        ).strip()
        mood_res = None
        if self.mood_engine_enabled and MOOD_ENGINE.enabled and mood_source_url.lower().startswith("https://"):
            mood_res = MOOD_ENGINE.resolve_for_url(mood_source_url, background=True)
            if mood_res is not None:
                variables["mood_badge"] = mood_res.mood_badge
                variables["mood_name"] = mood_res.mood_name

        spec = rule.presence
        start, end = self._timestamps(rule, activity)

        buttons: list[tuple[str, str]] = []
        if self.show_buttons:
            for button in rule.buttons:
                url = render(button.url, variables)
                label = render(button.label, variables)
                if not url or not label:
                    continue
                buttons.append((label, url))

        clean_buttons = sanitize_buttons(buttons)
        details_text = render_first(spec.details, variables)
        state_text = render_first(spec.state, variables)
        large_image = render_first(spec.large_image, variables)
        large_text = render_first(spec.large_text, variables)
        small_image = render_first(spec.small_image, variables)
        small_text = render_first(spec.small_text, variables)

        if rule.id == "browser-youtube":
            yt_thumb = variables.get("youtube_thumbnail", "").strip()
            if yt_thumb.lower().startswith("https://") and len(yt_thumb) <= 256:
                large_image = yt_thumb
            elif not large_image:
                large_image = "youtube"
            if not large_text:
                app_label = variables.get("app_label", "").strip()
                large_text = f"YouTube on {app_label}" if app_label else "YouTube"
        elif rule.id == "browser-github":
            gh_avatar = variables.get("github_avatar", "").strip()
            if gh_avatar.lower().startswith("https://") and len(gh_avatar) <= 256:
                large_image = gh_avatar
            elif not large_image:
                large_image = "github"

        if rule.kind == "media":
            dynamic_art = variables.get("artwork_url", "").strip()
            if dynamic_art.lower().startswith("https://") and len(dynamic_art) <= 256:
                large_image = dynamic_art
            elif not large_image:
                large_image = "spotify" if "spotify" in rule.id else "music_placeholder"

            if not large_text:
                track = variables.get("track", "").strip()
                artist = variables.get("artist", "").strip()
                if track and artist:
                    large_text = f"{track} by {artist}"
                elif track:
                    large_text = track

        if mood_res is not None and self.mood_apply_to_small_badge and (rule.kind == "media" or rule.id == "browser-youtube"):
            small_image = mood_res.mood_badge
            small_text = f"{small_text} \u2022 {mood_res.mood_name}" if small_text else mood_res.mood_name

        # Tier P6 IDLE / Standby integration with Hardware Telemetry & Celestial/Weather Sync
        if rule.tier == Tier.IDLE or rule.kind in ("afk", "standby"):
            try:
                cpu_pct = int(round(float(variables.get("cpu_percent") or variables.get("cpu") or 0)))
            except (TypeError, ValueError):
                cpu_pct = 0
            try:
                ram_used = float(variables.get("ram_used_gb") or 0.0)
            except (TypeError, ValueError):
                ram_used = 0.0
            try:
                ram_total = float(variables.get("ram_total_gb") or 16.0)
            except (TypeError, ValueError):
                ram_total = 16.0

            details_text = f"CPU: {cpu_pct}% \u2022 RAM: {ram_used:.1f} / {ram_total:.0f} GB"
            small_image = "cpu_chip" if cpu_pct >= 50 else "ram_chip"
            small_text = "OmniPresence v2.3.0"

            live_settings = SETTINGS_MANAGER.settings
            show_city = (
                bool(self.privacy_show_city)
                if self.privacy_show_city is not None
                else bool(getattr(live_settings, "privacy_show_city", False))
            )
            custom_idle_img = (
                self.idle_custom_image
                if self.idle_custom_image is not None
                else str(getattr(live_settings, "idle_custom_image", "") or "")
            ).strip()

            if self.celestial_sync_enabled and CELESTIAL_SYNC.enabled:
                city_name = (variables.get("city_name") or "").strip()
                raw_temp = str(variables.get("temp_c") or "24").replace("\u00b0C", "").strip() or "24"
                weather_desc = (variables.get("weather_desc") or "Clear Sky").strip()
                ambient_vibe = (variables.get("ambient_vibe") or "Starlit Night").strip()
                celestial_icon = (variables.get("celestial_icon") or "celestial_night").strip()

                if show_city and city_name:
                    state_text = f"{city_name} \u2022 {raw_temp}\u00b0C {weather_desc}"
                else:
                    state_text = f"{raw_temp}\u00b0C \u2022 {weather_desc}"
                large_image = custom_idle_img if custom_idle_img else celestial_icon
                large_text = f"System Idle \u2022 {ambient_vibe}"
            else:
                large_image = custom_idle_img or large_image or "idle"
                large_text = large_text or "System Idle"
        elif self.celestial_sync_enabled and CELESTIAL_SYNC.enabled:
            ambient_vibe = variables.get("ambient_vibe", "").strip()
            if self.celestial_append_to_state and ambient_vibe and small_text and ambient_vibe not in small_text:
                small_text = f"{small_text} \u2022 {ambient_vibe}"

        return PresenceData(
            details=_discord_text(details_text),
            state=_discord_text(state_text),
            large_image=large_image,
            large_text=_discord_text(large_text),
            small_image=small_image,
            small_text=_discord_text(small_text),
            start=start,
            end=end,
            buttons=clean_buttons,
            activity_type=spec.activity_type,
            tier=int(rule.tier),
            rule_id=rule.id,
        )

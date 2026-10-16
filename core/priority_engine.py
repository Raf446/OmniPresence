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

_TOKEN = re.compile(r"\[\[(.*?)\]\]|\{([A-Za-z_][A-Za-z0-9_]*)\}", re.S)
_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_OPTIONAL = re.compile(r"\[\[.*?\]\]", re.S)
_SPACES = re.compile(r"\s+")


def _lookup(name: str, variables: Mapping[str, str]) -> str:
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
        if self.buttons:
            kwargs["buttons"] = [{"label": label, "url": url} for label, url in self.buttons]
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
                    found = pattern.search(window.title)
                    if found:
                        groups = {k: (v or "").strip() for k, v in found.groupdict().items()}
                        break
                else:
                    continue
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
    manual: tuple[str, str] | None
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
    ) -> None:
        self.rulebook = rulebook
        self.idle_threshold = idle_threshold
        self.show_standby = show_standby
        self.show_buttons = show_buttons
        self._session_start: dict[str, float] = {}
        self._session_seen: dict[str, float] = {}

    def configure(self, idle_threshold: float, show_standby: bool, show_buttons: bool) -> None:
        self.idle_threshold = idle_threshold
        self.show_standby = show_standby
        self.show_buttons = show_buttons

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

        rule, activity = min(
            candidates,
            key=lambda pair: (pair[0].tier, 0 if pair[1].foreground else 1, book.order_of(pair[0].id)),
        )
        self._touch_sessions(candidates, inp.now)
        presence = self._render(rule, activity, shared, inp.now)

        outranked = len(candidates) - 1
        reason = f"P{int(rule.tier)} {rule.tier.label} via '{rule.id}'"
        if outranked:
            reason += f", outranks {outranked} other live candidate" + ("s" if outranked > 1 else "")
        if inp.paused:
            return Resolution(presence, rule.tier, rule, activity, live_tiers, live_rules, False, "paused: " + reason)
        return Resolution(presence, rule.tier, rule, activity, live_tiers, live_rules, True, reason)

    def _collect(
        self, inp: EngineInput, book: RuleBook, shared: Mapping[str, str]
    ) -> list[tuple[Rule, Activity]]:
        found: list[tuple[Rule, Activity]] = []
        for activity in inp.activities:
            rule = book.get(activity.rule_id)
            if rule is not None and rule.enabled:
                found.append((rule, activity))

        media = inp.media
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

        if inp.manual is not None:
            details, state = inp.manual
            for rule in book.of_kind("manual"):
                variables = {**shared, "manual_details": details, "manual_state": state}
                found.append((rule, Activity(rule.id, rule.id, variables)))
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

    def _render(
        self, rule: Rule, activity: Activity, shared: Mapping[str, str], now: float
    ) -> PresenceData:
        variables = {**shared, **activity.variables}
        spec = rule.presence
        start, end = self._timestamps(rule, activity)

        buttons: list[tuple[str, str]] = []
        if self.show_buttons:
            for button in rule.buttons:
                url = render(button.url, variables)
                label = render(button.label, variables)
                if not url or not label or not url.lower().startswith(("http://", "https://")):
                    continue
                if len(url) > DISCORD_URL_LIMIT:
                    continue
                buttons.append((label[:DISCORD_LABEL_LIMIT], url))

        large_image = render_first(spec.large_image, variables)
        large_text = render_first(spec.large_text, variables)

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

        return PresenceData(
            details=_discord_text(render_first(spec.details, variables)),
            state=_discord_text(render_first(spec.state, variables)),
            large_image=large_image,
            large_text=_discord_text(large_text),
            small_image=render_first(spec.small_image, variables),
            small_text=_discord_text(render_first(spec.small_text, variables)),
            start=start,
            end=end,
            buttons=tuple(buttons),
            activity_type=spec.activity_type,
        )

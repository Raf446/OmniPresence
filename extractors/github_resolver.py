"""Asynchronous, non-blocking GitHub user/organization avatar resolver (no API key required)."""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

GITHUB_AVATAR_TEMPLATE = "https://github.com/{username}.png?size=512"
DEFAULT_FALLBACK_ASSET = "github"
DEFAULT_GITHUB_USER = "Raf446"
HTTP_TIMEOUT_SECONDS = 1.5
NEGATIVE_CACHE_TTL_SECONDS = 60.0

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Valid GitHub username/org syntax: 1..39 alphanumeric or single hyphens, cannot start/end with hyphen
_GITHUB_USER_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_LEADING_NOISE_RE = re.compile(
    r"^(?:(?:\(\d+\)|\[\d+\])\s*|(?:[▶►▸▹⏵⏸⏹⏺🔊🔉🔈🔇🔴🟢🎵🎶🎧📺]\ufe0f?|\u25b6\ufe0f?|\u23f8\ufe0f?)\s*)+"
)
_REPO_SLUG_RE = re.compile(r"(?:^|[\s·•/-])(?P<owner>[a-zA-Z0-9_-]{1,39})/(?P<repo>[a-zA-Z0-9_.-]{1,100})")
_PAREN_PROFILE_RE = re.compile(
    r"^(?:.*?[·•\-\s])?(?P<prefix>[^()·•\-]+?)\s*\((?P<inside>[^()]+)\)(?:\s*[·•\-\—]\s*GitHub\b.*)?$"
)
_OWNER_ONLY_TITLE_RE = re.compile(
    r"^(?P<owner>[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38})\s*[·•\-\—]\s*GitHub\b"
)

RESERVED_GITHUB_NAMES = frozenset(
    {
        "github",
        "pulls",
        "issues",
        "marketplace",
        "explore",
        "topics",
        "trending",
        "collections",
        "events",
        "sponsors",
        "notifications",
        "settings",
        "login",
        "signup",
        "orgs",
        "users",
        "search",
        "new",
        "codespaces",
        "discussions",
        "features",
        "enterprise",
        "pricing",
        "security",
        "about",
        "gist",
        "readme",
        "customer-stories",
        "repositories",
        "projects",
        "packages",
        "stars",
    }
)


def extract_github_owner(raw_input: str) -> str:
    """Extract a clean GitHub username/owner from a username, 'owner/repo', or browser tab title."""
    if not raw_input:
        return ""
    cleaned = _ZERO_WIDTH_RE.sub("", raw_input)
    cleaned = _LEADING_NOISE_RE.sub("", cleaned).strip()
    if not cleaned:
        return ""

    # Strip leading '@' if passed as '@username'
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()

    # Case 1: Direct single-token username
    if "/" not in cleaned and " " not in cleaned and "·" not in cleaned and "(" not in cleaned:
        if _is_valid_owner(cleaned):
            return cleaned

    # Case 2: "owner/repo" slug anywhere in the string
    slug_match = _REPO_SLUG_RE.search(cleaned)
    if slug_match:
        owner = slug_match.group("owner").strip()
        if _is_valid_owner(owner):
            return owner

    # Case 3: Profile tab with parentheses: "<Display Name> (<Username>) · GitHub" or "<Username> (<Full Name>) · GitHub"
    paren_match = _PAREN_PROFILE_RE.match(cleaned)
    if paren_match:
        inside = paren_match.group("inside").strip()
        prefix = paren_match.group("prefix").strip()
        if inside.lower() == DEFAULT_GITHUB_USER.lower():
            return DEFAULT_GITHUB_USER
        if prefix.lower() == DEFAULT_GITHUB_USER.lower():
            return DEFAULT_GITHUB_USER
        if _is_valid_owner(inside):
            return inside
        if _is_valid_owner(prefix):
            return prefix

    # Case 4: Single-token profile tab title "<Username> · GitHub"
    prof_match = _OWNER_ONLY_TITLE_RE.search(cleaned)
    if prof_match:
        owner = prof_match.group("owner").strip()
        if _is_valid_owner(owner):
            return owner

    return ""


def _is_valid_owner(owner: str) -> bool:
    if not owner or owner.lower() in RESERVED_GITHUB_NAMES:
        return False
    return bool(_GITHUB_USER_RE.match(owner))


class _AvatarSignals(QObject):
    """Qt signals emitted when a background QThreadPool GitHub avatar lookup completes."""

    resolved = Signal(str, str, str)  # (normalized_key, username, avatar_url)


class _AvatarRunnable(QRunnable):
    """QThreadPool worker task that validates/resolves a GitHub avatar URL without blocking UI/IPC."""

    def __init__(
        self,
        resolver: "GitHubAvatarResolver",
        norm_key: str,
        username: str,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._resolver = resolver
        self._norm_key = norm_key
        self._username = username

    def run(self) -> None:
        url = DEFAULT_FALLBACK_ASSET
        try:
            url = self._resolver.fetch_sync(self._username)
        except Exception:
            url = DEFAULT_FALLBACK_ASSET
        finally:
            self._resolver._on_task_finished(self._norm_key, self._username, url, self)


class GitHubAvatarResolver:
    """Resolves GitHub usernames/titles to public avatar URLs via QThreadPool with in-memory caching."""

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS, verify_network: bool = False) -> None:
        self._timeout = min(max(timeout, 0.4), HTTP_TIMEOUT_SECONDS)
        self._verify_network = verify_network
        self._lock = threading.Lock()
        self.cache: dict[str, str] = {}
        self._failed_at: dict[str, float] = {}
        self._pending: set[str] = set()
        self._active_runnables: set[_AvatarRunnable] = set()
        self._listeners: list[Callable[[str, str], None]] = []
        self.signals = _AvatarSignals()
        self._pool = QThreadPool.globalInstance()

    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def get_cached(self, owner_or_title: str) -> tuple[bool, str]:
        """Return (is_cached, avatar_url_or_fallback) without network I/O."""
        owner = extract_github_owner(owner_or_title)
        if not owner:
            return True, DEFAULT_FALLBACK_ASSET
        key = owner.lower()
        now = time.monotonic()
        with self._lock:
            if key in self.cache:
                return True, self.cache[key]
            failed_ts = self._failed_at.get(key)
            if failed_ts is not None:
                if (now - failed_ts) < NEGATIVE_CACHE_TTL_SECONDS:
                    return True, DEFAULT_FALLBACK_ASSET
                del self._failed_at[key]
        return False, DEFAULT_FALLBACK_ASSET

    def fetch_sync(self, owner_or_title: str) -> str:
        """Resolve a GitHub owner/title to https://github.com/{username}.png?size=512 or 'github'."""
        owner = extract_github_owner(owner_or_title)
        if not owner:
            return DEFAULT_FALLBACK_ASSET
        key = owner.lower()

        with self._lock:
            if key in self.cache:
                return self.cache[key]

        avatar_url = GITHUB_AVATAR_TEMPLATE.format(username=owner)

        if self._verify_network:
            req = urllib.request.Request(
                avatar_url,
                method="HEAD",
                headers={"User-Agent": BROWSER_USER_AGENT},
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    if resp.status >= 400:
                        self._record_failure(key)
                        return DEFAULT_FALLBACK_ASSET
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._record_failure(key)
                    return DEFAULT_FALLBACK_ASSET
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                # Graceful degradation: deterministic GitHub PNG URL remains valid even if HEAD times out
                pass

        with self._lock:
            self.cache[key] = avatar_url
            self._failed_at.pop(key, None)
        return avatar_url

    def resolve(self, owner_or_title: str, *, background: bool = False) -> str:
        """Resolve a GitHub username or tab title to its avatar URL.

        - When `background=True`, checks in-memory cache first and dispatches a `QThreadPool` task
          on cache miss, returning immediately without blocking the caller.
        - When `background=False`, resolves synchronously and returns the avatar URL or `'github'`.
        """
        if background:
            return self.resolve_async(owner_or_title)
        return self.fetch_sync(owner_or_title)

    def resolve_async(self, owner_or_title: str) -> str:
        """Non-blocking lookup: return cached avatar URL immediately or dispatch a QThreadPool worker."""
        owner = extract_github_owner(owner_or_title)
        if not owner:
            return DEFAULT_FALLBACK_ASSET

        cached, url = self.get_cached(owner)
        if cached:
            return url

        key = owner.lower()
        with self._lock:
            if key in self._pending:
                return GITHUB_AVATAR_TEMPLATE.format(username=owner)
            self._pending.add(key)
            runnable = _AvatarRunnable(self, key, owner)
            self._active_runnables.add(runnable)

        self._pool.start(runnable)
        return GITHUB_AVATAR_TEMPLATE.format(username=owner)

    def _record_failure(self, key: str) -> None:
        with self._lock:
            self._failed_at[key] = time.monotonic()

    def _on_task_finished(
        self,
        norm_key: str,
        username: str,
        url: str,
        runnable: _AvatarRunnable,
    ) -> None:
        with self._lock:
            self._pending.discard(norm_key)
            self._active_runnables.discard(runnable)
            listeners = list(self._listeners)
        if url and url != DEFAULT_FALLBACK_ASSET:
            try:
                self.signals.resolved.emit(norm_key, username, url)
            except RuntimeError:
                pass
            for callback in listeners:
                try:
                    callback(username, url)
                except Exception:
                    pass


GITHUB_AVATAR_RESOLVER = GitHubAvatarResolver(verify_network=True)

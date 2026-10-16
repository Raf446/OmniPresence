"""Reads repository state straight from the .git directory, no subprocess involved."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SECTION = re.compile(r'^\s*\[\s*([\w.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_KEYVAL = re.compile(r"^\s*([\w.-]+)\s*=\s*(.*?)\s*$")
_SCP_REMOTE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$")

_ROOT_TTL = 15.0
_ROOT_SEARCH_DEPTH = 16


@dataclass(frozen=True)
class GitInfo:
    root: Path
    branch: str
    detached: bool
    head_sha: str
    remote_name: str
    remote_url: str
    repo_slug: str
    state: str
    ahead: int

    @property
    def sync_text(self) -> str:
        return {
            "synced": "up to date",
            "ahead": f"{self.ahead} ahead",
            "behind": "behind upstream",
            "diverged": "out of sync",
            "no_upstream": "no upstream",
            "detached": "detached HEAD",
            "unborn": "no commits",
        }.get(self.state, "")

    @property
    def sync_arrows(self) -> str:
        return {
            "ahead": f"\u2191{self.ahead}",
            "behind": "\u2193",
            "diverged": "\u2191\u2193",
        }.get(self.state, "")


@dataclass(frozen=True)
class _Dirs:
    root: Path
    git_dir: Path
    common_dir: Path


def find_repo_root(start: Path, max_depth: int = _ROOT_SEARCH_DEPTH) -> Path | None:
    """Nearest ancestor (or self) holding a .git directory or gitfile."""
    try:
        current = start if start.is_dir() else start.parent
    except OSError:
        return None
    for _ in range(max_depth):
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


def normalize_remote(url: str) -> tuple[str, str]:
    """Return (https browse URL, owner/name slug), or empty strings for local remotes."""
    url = url.strip()
    if not url:
        return "", ""
    host = path = ""
    if "://" in url:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.hostname:
            host, path = parsed.hostname, parsed.path
    else:
        scp = _SCP_REMOTE.match(url)
        if scp and not re.match(r"^[A-Za-z]:[\\/]", url):
            host, path = scp.group(1), scp.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return "", ""
    return f"https://{host}/{path}", path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _resolve_dirs(root: Path) -> _Dirs | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return _Dirs(root, dot_git, dot_git)
    text = _read_text(dot_git).strip()
    if not text.startswith("gitdir:"):
        return None
    git_dir = Path(text[len("gitdir:"):].strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    common = git_dir
    pointer = _read_text(git_dir / "commondir").strip()
    if pointer:
        common = (git_dir / pointer).resolve()
    return _Dirs(root, git_dir, common)


def _packed_ref(common_dir: Path, ref: str) -> str:
    for line in _read_text(common_dir / "packed-refs").splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA.match(sha):
            return sha
    return ""


def _read_ref(dirs: _Dirs, ref: str) -> str:
    for base in (dirs.common_dir, dirs.git_dir):
        value = _read_text(base / ref).strip()
        if _SHA.match(value):
            return value
    return _packed_ref(dirs.common_dir, ref)


def _read_reflog(path: Path) -> list[tuple[str, str]]:
    entries = []
    for line in _read_text(path).splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and _SHA.match(parts[0]) and _SHA.match(parts[1]):
            entries.append((parts[0], parts[1]))
    return entries


def _parse_config(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    sections: dict[tuple[str, str], dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in _read_text(path).splitlines():
        header = _SECTION.match(line)
        if header:
            current = sections.setdefault((header.group(1).lower(), header.group(2) or ""), {})
            continue
        pair = _KEYVAL.match(line)
        if pair and current is not None and not line.lstrip().startswith(("#", ";")):
            current.setdefault(pair.group(1).lower(), pair.group(2).strip('"'))
    return sections


def _sync_state(
    local_sha: str, remote_sha: str, local_log: list[tuple[str, str]], remote_log: list[tuple[str, str]]
) -> tuple[str, int]:
    """Ahead/behind estimated from reflogs, since commit graphs need object parsing.

    Ahead counts local reflog entries made after the branch last sat on the remote tip.
    Behind is only detected, not counted: one fetch can bring many commits.
    """
    if local_sha == remote_sha:
        return "synced", 0
    count = 0
    for _, new in reversed(local_log):
        if new == remote_sha:
            if count:
                return "ahead", count
            break
        count += 1
    if any(new == local_sha for _, new in remote_log):
        return "behind", 0
    return "diverged", 0


class GitExtractor:
    """Caches per-repository results and refreshes them when watched files change."""

    def __init__(self) -> None:
        self._roots: dict[Path, tuple[Path | None, float]] = {}
        self._cache: dict[Path, tuple[tuple, GitInfo]] = {}

    def root_for(self, path: Path) -> Path | None:
        now = time.monotonic()
        cached = self._roots.get(path)
        if cached and now - cached[1] < _ROOT_TTL:
            return cached[0]
        root = find_repo_root(path)
        self._roots[path] = (root, now)
        return root

    def inspect(self, path: Path) -> GitInfo | None:
        root = self.root_for(path)
        if root is None:
            return None
        dirs = _resolve_dirs(root)
        if dirs is None:
            return None
        head_text = _read_text(dirs.git_dir / "HEAD").strip()
        branch = head_text[len("ref: refs/heads/"):] if head_text.startswith("ref: refs/heads/") else ""

        cached = self._cache.get(root)
        remote_hint = cached[1].remote_name if cached else "origin"
        signature = self._signature(dirs, head_text, branch, remote_hint)
        if cached and cached[0] == signature:
            return cached[1]

        info = self._build(dirs, head_text, branch)
        self._cache[root] = (self._signature(dirs, head_text, branch, info.remote_name or "origin"), info)
        return info

    @staticmethod
    def _signature(dirs: _Dirs, head_text: str, branch: str, remote: str) -> tuple:
        watched = [dirs.common_dir / "config", dirs.common_dir / "packed-refs"]
        if branch:
            watched += [
                dirs.common_dir / "refs" / "heads" / branch,
                dirs.common_dir / "logs" / "refs" / "heads" / branch,
                dirs.common_dir / "refs" / "remotes" / remote / branch,
                dirs.common_dir / "logs" / "refs" / "remotes" / remote / branch,
            ]
        return (head_text, tuple(_mtime(p) for p in watched))

    def _build(self, dirs: _Dirs, head_text: str, branch: str) -> GitInfo:
        config = _parse_config(dirs.common_dir / "config")
        detached = not branch
        head_sha = _read_ref(dirs, head_text[5:]) if head_text.startswith("ref: ") else head_text
        if not _SHA.match(head_sha):
            head_sha = ""

        branch_cfg = config.get(("branch", branch), {}) if branch else {}
        remote = branch_cfg.get("remote") or "origin"
        remote_url = config.get(("remote", remote), {}).get("url", "")
        browse_url, slug = normalize_remote(remote_url)

        if detached:
            label = head_sha[:7] or "HEAD"
            return GitInfo(dirs.root, label, True, head_sha, remote, browse_url, slug, "detached", 0)
        if not head_sha:
            return GitInfo(dirs.root, branch, False, "", remote, browse_url, slug, "unborn", 0)

        upstream = branch_cfg.get("merge", f"refs/heads/{branch}")
        upstream_name = upstream[len("refs/heads/"):] if upstream.startswith("refs/heads/") else branch
        remote_sha = _read_ref(dirs, f"refs/remotes/{remote}/{upstream_name}")
        if not remote_sha:
            return GitInfo(dirs.root, branch, False, head_sha, remote, browse_url, slug, "no_upstream", 0)

        local_log = _read_reflog(dirs.common_dir / "logs" / "refs" / "heads" / branch)
        remote_log = _read_reflog(dirs.common_dir / "logs" / "refs" / "remotes" / remote / upstream_name)
        state, ahead = _sync_state(head_sha, remote_sha, local_log, remote_log)
        return GitInfo(dirs.root, branch, False, head_sha, remote, browse_url, slug, state, ahead)

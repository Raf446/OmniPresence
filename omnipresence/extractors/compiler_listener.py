"""Detects running builds from the process table and works out what is being built."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import psutil

DRIVERS = frozenset({"make", "mingw32-make", "gmake", "nmake", "ninja", "cargo", "cmake", "msbuild"})
COMPILERS = frozenset({"cl", "rustc", "javac", "gcc", "g++", "c++", "cc", "clang", "clang++", "gfortran"})
INTERNALS = frozenset({"cc1", "cc1plus", "collect2", "ld", "as", "lto-wrapper", "link"})

_TOOLCHAIN = re.compile(r"^(?:[a-z0-9_.]+-)*?(gcc|g\+\+|c\+\+|cc|clang\+\+|clang|gfortran)(?:-\d+(?:\.\d+)*)?$")
_SOURCE_EXT = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm", ".s", ".f", ".f90", ".rs", ".java"})
_INTERMEDIATE_EXT = frozenset({".o", ".obj", ".s", ".i", ".ii"})
_VALUE_FLAGS = frozenset(
    {"-I", "-L", "-D", "-U", "-l", "-include", "-isystem", "-MF", "-MT", "-MQ", "-x", "-T",
     "-Xlinker", "-imacros", "-idirafter", "-iquote", "-B", "-aux-info"}
)
_GENERIC_DIRS = frozenset(
    {"build", "out", "bin", "obj", "debug", "release", "target", "dist", "cmake-build-debug", "cmake-build-release"}
)
_BUILD_SUBCOMMANDS = frozenset({"build", "run", "check", "test", "clippy", "doc", "bench", "install"})


class ProcessLike(Protocol):
    pid: int
    ppid: int
    name: str
    create_time: float


@dataclass(frozen=True)
class BuildInfo:
    tool: str
    target: str
    project: str
    cwd: str
    started_at: float


@dataclass(frozen=True)
class _Detail:
    args: tuple[str, ...]
    cwd: str


def canonical_tool(process_name: str) -> str:
    stem = process_name.lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    if stem in DRIVERS or stem in COMPILERS or stem in INTERNALS:
        return stem
    match = _TOOLCHAIN.match(stem)
    return match.group(1) if match else ""


def project_name(cwd: str) -> str:
    if not cwd:
        return ""
    path = Path(cwd)
    while path.name.lower() in _GENERIC_DIRS and path.parent != path:
        path = path.parent
    return path.name


def _compiler_target(tool: str, args: list[str], cwd: str) -> str:
    output = ""
    sources: list[str] = []
    skip_next = False
    for position, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if tool == "cl" and arg[:1] in "/-":
            lowered = arg.lower()
            for prefix in ("/fe:", "/fe", "/out:"):
                if lowered.startswith(prefix) and len(arg) > len(prefix):
                    output = arg[len(prefix):]
            continue
        if arg == "-o" and position + 1 < len(args):
            output = args[position + 1]
            skip_next = True
        elif arg in _VALUE_FLAGS:
            skip_next = True
        elif arg.startswith("-") or arg.startswith("@"):
            continue
        elif Path(arg).suffix.lower() in _SOURCE_EXT:
            sources.append(arg)
    if output and Path(output).suffix.lower() not in _INTERMEDIATE_EXT:
        return Path(output).name
    if sources:
        return Path(sources[0]).name
    return project_name(cwd) or tool


def _rustc_target(args: list[str], cwd: str) -> str:
    for position, arg in enumerate(args):
        if arg == "--crate-name" and position + 1 < len(args):
            return args[position + 1]
        if arg.startswith("--crate-name="):
            return arg.split("=", 1)[1]
    for arg in args:
        if arg.endswith(".rs"):
            return Path(arg).stem
    return project_name(cwd) or "rustc"


def _driver_target(tool: str, args: list[str], cwd: str) -> str:
    directory = cwd
    positional: list[str] = []
    skip_next = False
    for position, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg in {"-C", "--directory"} and position + 1 < len(args):
            directory = args[position + 1] if Path(args[position + 1]).is_absolute() else str(Path(cwd) / args[position + 1])
            skip_next = True
        elif arg.startswith("--directory="):
            directory = arg.split("=", 1)[1]
        elif arg in {"-f", "-j", "-p", "--package", "--manifest-path", "--target-dir"} or (
            tool == "cmake" and arg in {"-S", "-B", "-G", "-A", "-T"}
        ):
            skip_next = True
        elif arg == "--build" and position + 1 < len(args):
            directory = args[position + 1]
            skip_next = True
        elif arg.startswith("-") or "=" in arg:
            continue
        elif tool == "msbuild" and Path(arg).suffix.lower() in {".sln", ".csproj", ".vcxproj"}:
            return Path(arg).stem
        elif tool == "cargo" and arg in _BUILD_SUBCOMMANDS:
            continue
        else:
            positional.append(arg)
    if tool in {"make", "mingw32-make", "gmake", "ninja", "nmake"} and positional:
        return ", ".join(positional[:2])
    return project_name(directory) or tool


def extract_target(tool: str, args: list[str], cwd: str) -> str:
    if tool in DRIVERS:
        return _driver_target(tool, args, cwd)
    if tool == "rustc":
        return _rustc_target(args, cwd)
    if tool == "javac":
        for arg in args:
            if arg.endswith(".java"):
                return Path(arg).name
        return project_name(cwd) or tool
    return _compiler_target(tool, args, cwd)


def _rank(tool: str) -> int:
    if tool in DRIVERS:
        return 0
    if tool in INTERNALS:
        return 2
    return 1


class CompilerListener:
    """Turns the process table into a single BuildInfo.

    Build tools live for a fraction of a second, so the last result is held for
    `linger_seconds` after the final tool exits.
    """

    def __init__(self, linger_seconds: float = 4.0) -> None:
        self.linger_seconds = linger_seconds
        self._details: dict[int, _Detail] = {}
        self._last: BuildInfo | None = None
        self._last_seen = 0.0

    def poll(self, records: Iterable[ProcessLike], now: float | None = None) -> BuildInfo | None:
        now = time.time() if now is None else now
        active: list[tuple[ProcessLike, str]] = []
        for record in records:
            tool = canonical_tool(record.name)
            if tool:
                active.append((record, tool))

        live_pids = {record.pid for record, _ in active}
        for pid in [p for p in self._details if p not in live_pids]:
            del self._details[pid]

        if not active:
            if self._last and now - self._last_seen <= self.linger_seconds:
                return self._last
            self._last = None
            return None

        for record, _ in active:
            if record.pid not in self._details:
                self._details[record.pid] = self._inspect(record.pid)

        lead, tool = min(active, key=lambda pair: (_rank(pair[1]), pair[0].create_time))
        detail = self._details[lead.pid]
        started = min(record.create_time for record, _ in active)
        if self._last and now - self._last_seen <= self.linger_seconds:
            started = min(started, self._last.started_at)

        if tool in INTERNALS:
            tool = "gcc"
        cwd = detail.cwd or (self._last.cwd if self._last else "")
        info = BuildInfo(
            tool=tool,
            target=extract_target(tool, list(detail.args), cwd),
            project=project_name(cwd),
            cwd=cwd,
            started_at=started,
        )
        self._last, self._last_seen = info, now
        return info

    @staticmethod
    def _inspect(pid: int) -> _Detail:
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                args = tuple(process.cmdline()[1:])
                try:
                    cwd = process.cwd()
                except (psutil.AccessDenied, OSError):
                    cwd = ""
            return _Detail(args, cwd)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return _Detail((), "")

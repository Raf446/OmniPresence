"""Core engine package for OmniPresence: priority arbitration, process watching, IPC, and state."""
from __future__ import annotations

from core.ipc_manager import ConnectionState, IPCManager
from core.priority_engine import (
    Activity,
    EngineInput,
    PresenceData,
    PriorityEngine,
    Resolution,
    Rule,
    RuleBook,
    RuleError,
    Tier,
    WindowFacts,
)
from core.process_watcher import ProcessWatcher, ScanResult
from core.state_manager import LogEntry, Settings, StateManager

__all__ = [
    "Activity",
    "ConnectionState",
    "EngineInput",
    "IPCManager",
    "LogEntry",
    "PresenceData",
    "PriorityEngine",
    "ProcessWatcher",
    "Resolution",
    "Rule",
    "RuleBook",
    "RuleError",
    "ScanResult",
    "Settings",
    "StateManager",
    "Tier",
    "WindowFacts",
]


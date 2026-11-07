"""Execution-plane boundary; implementations must enforce isolation before execution."""
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class SandboxSession:
    session_id: str
    run_id: str
    base_commit: str

class SandboxProvider(Protocol):
    def start(self, run_id: str, base_commit: str, repo_archive: bytes) -> SandboxSession: ...
    def execute_tool(self, session_id: str, tool: str, args: dict) -> dict: ...
    def analyze(self, session_id: str, contract: dict) -> dict: ...
    def destroy(self, session_id: str) -> None: ...

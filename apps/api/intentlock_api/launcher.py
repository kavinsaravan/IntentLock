"""LOCAL-DEV agent launcher: lets the control plane start the execution plane's runner for a run.

This runs on the machine hosting the API (a local subprocess). It is NOT used by the Lambda and is not
a production orchestration design. The command is fixed by server configuration; requests can only
supply a run id. No shell is ever used. The launcher never simulates an agent: it starts whatever
command the operator configured (Person 2's `launch_kiro.py`, which supervises the real Kiro CLI).

Config (env):
  INTENTLOCK_AGENT_LAUNCH_ARGV   JSON array of argv strings. Placeholders: {run_id} {contract_id}
                                 {contract_version} {mission}. Use "--prompt={mission}" so a mission can
                                 never be parsed as an option.
  INTENTLOCK_API_URL             URL the runner uses to reach this API (passed to the child)
  INTENTLOCK_INTERNAL_TOKEN      passed to the child via ENV (never argv)
  INTENTLOCK_AGENT_CWD           working dir for the runner (optional)
  INTENTLOCK_AGENT_ENV_PASSTHROUGH  extra env names to forward (default list below; no AWS_* ever)
  INTENTLOCK_AGENT_LOG_DIR       where runner stdout/stderr go (default: a private temp dir; never served)
  INTENTLOCK_AGENT_STOP_GRACE_SECONDS  default 3 (see watcher)

Termination: the runner supervises the agent and exits when the run becomes terminal. As a backstop
the watcher escalates against the RUNNER if it is still alive after the grace period:
SIGINT (the runner handles KeyboardInterrupt and kills the agent group) -> SIGTERM -> SIGKILL of the
runner's process group. Exit code 0 => run `completed`; non-zero => `failed` (only if still active).
"""
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .store import now as _now

DEFAULT_PASSTHROUGH = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "KIRO_CLI_BINARY")
ALLOWED_PLACEHOLDERS = {"run_id", "contract_id", "contract_version", "mission"}


class LauncherNotConfigured(Exception):
    pass


class LaunchError(Exception):
    pass


class AlreadyLaunched(Exception):
    pass


@dataclass
class LaunchRecord:
    run_id: str
    process: subprocess.Popen
    started_at: datetime
    exit_code: int | None = None
    escalation: list[str] = field(default_factory=list)


class AgentLauncher:
    def __init__(self, store, env: dict[str, str] | None = None, now_fn: Callable[[], datetime] = _now) -> None:
        env = dict(os.environ if env is None else env)
        self._store, self._now = store, now_fn
        self._records: dict[str, LaunchRecord] = {}
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self.argv_template: list[str] | None = None
        self.problem: str | None = None
        raw = env.get("INTENTLOCK_AGENT_LAUNCH_ARGV")
        self._api_url = env.get("INTENTLOCK_API_URL", "")
        self._token = env.get("INTENTLOCK_INTERNAL_TOKEN", "")
        self._cwd = env.get("INTENTLOCK_AGENT_CWD") or None
        self._grace = float(env.get("INTENTLOCK_AGENT_STOP_GRACE_SECONDS", "3"))
        self._log_dir = env.get("INTENTLOCK_AGENT_LOG_DIR") or tempfile.mkdtemp(prefix="intentlock-agent-logs-")
        names = list(DEFAULT_PASSTHROUGH) + [n for n in env.get("INTENTLOCK_AGENT_ENV_PASSTHROUGH", "").split(",") if n]
        self._passthrough = {n: env[n] for n in names if n in env and not n.upper().startswith("AWS_")}
        if not raw:
            self.problem = "INTENTLOCK_AGENT_LAUNCH_ARGV is not set"
        elif not self._api_url or not self._token:
            self.problem = "INTENTLOCK_API_URL and INTENTLOCK_INTERNAL_TOKEN must be set"
        else:
            try:
                argv = json.loads(raw)
                if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
                    raise ValueError("must be a non-empty JSON array of strings")
                import string
                for a in argv:
                    for _, field_name, _, _ in string.Formatter().parse(a):
                        if field_name and field_name not in ALLOWED_PLACEHOLDERS:
                            raise ValueError(f"unknown placeholder {{{field_name}}}")
                self.argv_template = argv
            except ValueError as e:
                self.problem = f"INTENTLOCK_AGENT_LAUNCH_ARGV invalid: {e}"

    @property
    def configured(self) -> bool:
        return self.argv_template is not None

    # ------------------------------------------------------------------ launch
    def launch(self, run, contract) -> LaunchRecord:
        if not self.configured:
            raise LauncherNotConfigured(self.problem or "not configured")
        with self._lock:
            if run.run_id in self._records:
                raise AlreadyLaunched(run.run_id)
            values = {"run_id": run.run_id, "contract_id": run.contract_id,
                      "contract_version": str(run.contract_version), "mission": contract.mission}
            argv = [a.format(**values) for a in self.argv_template or []]
            child_env = dict(self._passthrough)
            child_env.update(INTENTLOCK_API_URL=self._api_url, INTENTLOCK_INTERNAL_TOKEN=self._token)
            try:
                os.makedirs(self._log_dir, mode=0o700, exist_ok=True)
                log = open(os.path.join(self._log_dir, f"{run.run_id}.log"), "ab")
                proc = subprocess.Popen(argv, cwd=self._cwd, env=child_env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                log.close()
            except OSError as e:
                raise LaunchError(type(e).__name__) from e
            rec = LaunchRecord(run.run_id, proc, self._now())
            self._records[run.run_id] = rec
        t = threading.Thread(target=self._watch, args=(rec,), daemon=True, name=f"agent-watch-{run.run_id}")
        self._threads.append(t)
        t.start()
        return rec

    # ------------------------------------------------------------------ status
    def status(self, run_id: str) -> dict[str, Any]:
        rec = self._records.get(run_id)
        if rec is None:
            return {"launched": False}
        rc = rec.process.poll()
        return {"launched": True, "launcher_pid": rec.process.pid, "alive": rc is None,
                "exit_code": rc, "started_at": rec.started_at.isoformat(), "escalation": list(rec.escalation)}

    def info(self) -> dict[str, Any]:
        return {"configured": self.configured, "mode": "local-subprocess" if self.configured else None,
                "detail": None if self.configured else self.problem}

    def shutdown(self, join_timeout: float = 5.0) -> None:
        """Stop watching, kill any runner still alive, and join the watcher threads. After this returns no
        watcher touches the store again (important for tests and for clean process exit)."""
        self._stopping.set()
        for rec in list(self._records.values()):
            if rec.process.poll() is None:
                try:
                    os.killpg(rec.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for t in self._threads:
            t.join(join_timeout)

    # ----------------------------------------------------------------- watcher
    def _signal_group(self, rec: LaunchRecord, sig: int, label: str) -> None:
        rec.escalation.append(label)
        try:
            os.killpg(rec.process.pid, sig)
        except ProcessLookupError:
            pass

    def _watch(self, rec: LaunchRecord) -> None:
        terminal_since: float | None = None
        steps = [(0.0, signal.SIGINT, "SIGINT"), (2.0, signal.SIGTERM, "SIGTERM"), (4.0, signal.SIGKILL, "SIGKILL")]
        done: set[str] = set()
        while rec.process.poll() is None and not self._stopping.is_set():
            try:
                run = self._store.get_run(rec.run_id)
            except Exception:
                run = None
            if run is not None and run.status not in ("created", "active"):
                terminal_since = terminal_since if terminal_since is not None else time.monotonic()
                waited = time.monotonic() - terminal_since
                for after, sig, label in steps:
                    if waited >= self._grace + after and label not in done:
                        done.add(label)
                        self._signal_group(rec, sig, label)
            time.sleep(0.1)
        if self._stopping.is_set():
            return                                   # shut down: never touch the store again
        rec.exit_code = rec.process.returncode
        self._record_exit(rec)

    def _record_exit(self, rec: LaunchRecord) -> None:
        code = rec.exit_code
        try:
            status = "completed" if code == 0 else "failed"
            won = self._store.transition_run(rec.run_id, ["created", "active"], status=status, completed_at=self._now(),
                                             stop_reason=None if code == 0 else f"agent launcher exited with code {code}")
            self._store.append_event(rec.run_id, event_type="agent_event", status="recorded",
                                     message=f"Agent launcher exited with code {code}",
                                     details={"exit_code": code, "escalation": list(rec.escalation),
                                              "run_state_changed_by_launcher": won is not None})
            if won is not None:
                self._store.append_event(rec.run_id, event_type="run_completed", status="recorded",
                                         message=f"Run {status} (launcher exit {code})", details={"status": status})
        except Exception:
            pass  # never raise from the watcher thread; state stays as last committed

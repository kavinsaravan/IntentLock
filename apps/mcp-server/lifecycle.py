"""Supervise Kiro, report terminal state, and clean up its process group."""
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from control_plane import ControlPlaneError


def _signal_process(process, sig):
    try:
        process.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_group(process, grace=1):
    """Best effort group cleanup; return False if group signaling was denied."""
    group_signaled = True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            group_signaled = False
            _signal_process(process, sig)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        _signal_process(process, signal.SIGKILL)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            return False
    return group_signaled


def supervise(command, cwd, client, run_id, contract_id, version, poll_seconds=0.2, max_seconds=600):
    """Only the execution plane reports completion; stop/blocked remain authoritative."""
    def active():
        state = client.run(run_id)
        if state['contract_id'] != contract_id or state['contract_version'] != version:
            raise ControlPlaneError('RUN_IDENTITY_MISMATCH')
        return state['status'] == 'active'

    if not active():
        raise ControlPlaneError('RUN_NOT_ACTIVE')
    interrupted = threading.Event()
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: interrupted.set())
    process = None
    report_status = None
    result_code = 2
    outcome = 'Agent terminated after control-plane failure or local interruption'
    try:
        process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
        client.register(run_id, process.pid)
        deadline = time.monotonic() + max_seconds
        while True:
            if interrupted.is_set():
                report_status = 'failed'
                outcome = 'Agent terminated after SIGTERM'
                break
            code = process.poll()
            if code is not None:
                report_status = 'completed' if code == 0 else 'failed'
                result_code = 0 if code == 0 else 2
                outcome = 'Agent completed' if code == 0 else f'Agent exited with code {code}'
                break
            if not active():
                outcome = 'Agent terminated after terminal run state'
                break
            if time.monotonic() >= deadline:
                report_status = 'failed'
                outcome = 'Agent terminated after local run timeout'
                break
            interrupted.wait(poll_seconds)
    except (KeyboardInterrupt, OSError):
        report_status = 'failed'
        outcome = 'Agent terminated after local interruption or launch failure'
    except ControlPlaneError:
        outcome = 'Agent terminated after control-plane failure or local interruption'
    finally:
        if process is not None and not terminate_group(process):
            report_status = 'failed'
            result_code = 2
            outcome = 'Agent process-group cleanup was denied'
        try:
            client.event(run_id, outcome)
        except ControlPlaneError:
            pass
        if report_status is not None:
            try:
                response = client.finish(run_id, report_status, outcome)
                if response['run']['status'] != report_status:
                    result_code = 2
            except ControlPlaneError:
                result_code = 2
        if os.environ.get('INTENTLOCK_ARTIFACT_BUCKET') or os.environ.get('INTENTLOCK_ARTIFACT_DIR'):
            try:
                import importlib.util
                source = Path(__file__).resolve().parents[2] / 'packages/sandbox-provider/export_audit.py'
                spec = importlib.util.spec_from_file_location('intentlock_export_audit', source)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.export(client, module.artifacts.from_env(), run_id)
            except Exception:
                # Audit export is best effort; never rewrite an authoritative run state.
                pass
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return result_code

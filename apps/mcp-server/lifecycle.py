"""Own the Kiro process group; stop on terminal state or lost control-plane access."""
import os
import signal
import subprocess
import time
from control_plane import ControlPlaneError


def terminate_group(process, grace=1):
    # Clean up MCP descendants even if Kiro already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def supervise(command, cwd, client, run_id, contract_id, version, poll_seconds=0.2, max_seconds=600):
    def active():
        state = client.run(run_id)
        if state['contract_id'] != contract_id or state['contract_version'] != version:
            raise ControlPlaneError('RUN_IDENTITY_MISMATCH')
        return state['status'] == 'active'

    if not active():
        raise ControlPlaneError('RUN_NOT_ACTIVE')
    process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    outcome = 'Agent exited'
    try:
        client.register(run_id, process.pid)
        deadline = time.monotonic() + max_seconds
        while process.poll() is None:
            if not active():
                outcome = 'Agent terminated after terminal run state'
                return 2
            if time.monotonic() >= deadline:
                outcome = 'Agent terminated after local run timeout'
                return 2
            time.sleep(poll_seconds)
        return process.returncode
    except (ControlPlaneError, KeyboardInterrupt):
        outcome = 'Agent terminated after control-plane failure or local interruption'
        return 2
    finally:
        terminate_group(process)
        try:
            client.event(run_id, outcome)
        except ControlPlaneError:
            pass

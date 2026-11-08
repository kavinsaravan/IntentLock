"""demo/start_local_demo.sh must refuse to start when a demo port is already taken.

Regression: with a stale stack on the demo ports, the new services failed to bind, the stale stack
answered the readiness probe, and the script printed "Demo ready" for a stack that was not running.
The port check runs before any dependency check, so these tests need no Docker, Kiro, or AWS.
"""
import os
import pathlib
import socket
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "demo" / "start_local_demo.sh"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_check(**ports):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    env.update({
        "INTENTLOCK_DEMO_API_PORT": str(ports["api"]),
        "INTENTLOCK_DEMO_PROMOTER_PORT": str(ports["promoter"]),
        "INTENTLOCK_DEMO_WEB_PORT": str(ports["web"]),
    })
    r = subprocess.run(["bash", str(SCRIPT), "--check"], env=env, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


@pytest.mark.parametrize("busy", ["api", "promoter", "web"])
def test_refuses_to_start_when_a_demo_port_is_in_use(busy):
    ports = {"api": free_port(), "promoter": free_port(), "web": free_port()}
    with socket.socket() as held:
        held.bind(("127.0.0.1", ports[busy]))
        held.listen()
        rc, out = run_check(**ports)
    assert rc == 2
    assert f"port {ports[busy]} ({busy}) is already in use" in out
    assert "Demo ready" not in out


def test_free_ports_pass_the_port_check():
    ports = {"api": free_port(), "promoter": free_port(), "web": free_port()}
    rc, out = run_check(**ports)
    # With free ports the script gets past the port check; it may still stop later on missing
    # tools or secrets in this minimal environment, but never because of a port.
    assert "is already in use" not in out

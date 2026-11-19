"""Keep the restricted PostgreSQL SSH forwarding process alive across job steps."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


TARGET = "coupon-tunnel@84.13.143.171"


def tunnel_paths():
    directory = Path(os.environ["RUNNER_TEMP"])
    return directory / "oracle_pg_tunnel.sock", directory / "oracle_pg_tunnel.pid"


def supervise(command, socket_path, stop):
    delay = 2
    while not stop.is_set():
        socket_path.unlink(missing_ok=True)
        started = time.monotonic()
        child = None
        try:
            child = subprocess.Popen(command)
            while not stop.is_set() and child.poll() is None:
                stop.wait(1)
        except OSError as error:
            print(f"SSH tunnel startup failed ({type(error).__name__}).", flush=True)
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if stop.is_set():
            break
        if time.monotonic() - started >= 60:
            delay = 2
        print(f"SSH tunnel disconnected; reconnecting in {delay} seconds.", flush=True)
        stop.wait(delay)
        delay = min(delay * 2, 30)
    socket_path.unlink(missing_ok=True)


def worker():
    socket_path, pid_path = tunnel_paths()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    ssh_directory = Path.home() / ".ssh"
    command = [
        "ssh", "-N", "-M", "-S", str(socket_path),
        "-L", "127.0.0.1:5432:127.0.0.1:5432",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ConnectionAttempts=1",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={ssh_directory / 'known_hosts'}",
        "-i", str(ssh_directory / "oracle_pg_key"), TARGET,
    ]
    try:
        supervise(command, socket_path, stop)
    finally:
        pid_path.unlink(missing_ok=True)


def stop_supervisor():
    _, pid_path = tunnel_paths()
    if pid_path.exists():
        try:
            os.kill(int(pid_path.read_text().strip()), signal.SIGTERM)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)


def start():
    socket_path, pid_path = tunnel_paths()
    if pid_path.exists():
        raise RuntimeError("An SSH tunnel supervisor is already registered.")
    log_path = socket_path.with_suffix(".log")
    with log_path.open("a") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "worker"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    pid_path.write_text(str(process.pid))
    for _ in range(60):
        if process.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise RuntimeError("SSH tunnel supervisor exited during startup.")
        try:
            check = subprocess.run(
                ["ssh", "-S", str(socket_path), "-O", "check", TARGET],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            if check.returncode == 0:
                print("SSH tunnel supervisor is ready.", flush=True)
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    stop_supervisor()
    raise RuntimeError("SSH forwarding did not become ready within the startup deadline.")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action == "start":
        start()
    elif action == "stop":
        stop_supervisor()
    elif action == "worker":
        worker()
    else:
        raise SystemExit("Usage: python ssh_tunnel.py start|stop")

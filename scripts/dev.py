"""Development process supervisor. Only stops process groups it created."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def _signal_group(child: subprocess.Popen, sig: int) -> None:
    # Only signal live children: avoids killing a reused PID after reap.
    try:
        if child.poll() is None:
            os.killpg(child.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop(children: list[subprocess.Popen], grace: float = 5.0) -> None:
    # A wrapper may exit before its children, so signal its group even if it exited.
    for child in children:
        _signal_group(child, signal.SIGTERM)
    # Wait in parallel so one slow service doesn't eat the other's grace period.
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if all(child.poll() is not None for child in children):
            break
        time.sleep(0.05)
    for child in children:
        _signal_group(child, signal.SIGKILL)
    for child in children:
        try:
            child.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def supervise(commands: list[tuple[list[str], Path]], env: dict[str, str]) -> int:
    children: list[subprocess.Popen] = []
    interrupted = False

    def shutdown(signum, frame):
        nonlocal interrupted
        if interrupted:
            # Second Ctrl+C / SIGTERM while cleaning up: force-kill immediately
            # instead of waiting out the grace period.
            for child in list(children):
                _signal_group(child, signal.SIGKILL)
            return
        interrupted = True

    previous = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for command, cwd in commands:
            if interrupted:
                return 0
            children.append(subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True))
        while not interrupted:
            for index, child in enumerate(children):
                code = child.poll()
                if code is not None:
                    print(f"[dev] Service {index + 1} exited ({code}); stopping the other service.", flush=True)
                    return code if code > 0 else 1
            time.sleep(0.15)
        return 0
    finally:
        stop(children)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
        return False


def _pids_listening(port: int) -> list[int]:
    """Return PIDs listening on TCP *port* (best effort, via lsof)."""
    try:
        proc = subprocess.run(
            ["lsof", "-tiTCP:{}".format(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        # lsof -t prints one PID per line; be strict to avoid killing by mistake.
        if line.isdigit():
            pid = int(line)
            if pid != os.getpid():
                pids.append(pid)
    return pids


def _kill_pids(pids: list[int], grace: float = 3.0) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Wait in parallel so one slow process doesn't eat the other's grace period.
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        alive = False
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                # Exists but we can't signal it; SIGKILL below will also fail
                # and check_ports() will report "still in use".
                alive = True
            else:
                alive = True
        if not alive:
            break
        time.sleep(0.05)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def check_ports(ports: list[int], force: bool = False) -> None:
    if len(set(ports)) != len(ports):
        raise ValueError("Frontend and backend must use different ports")
    for port in ports:
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port: {port}")
        if not _port_in_use(port):
            continue
        if not force:
            raise ValueError(f"Port {port} is already in use; stop that service or choose another port (or retry with --force)")
        pids = _pids_listening(port)
        if pids:
            print(f"[dev] --force: killing PID(s) {', '.join(map(str, pids))} on port {port}…", flush=True)
            _kill_pids(pids)
        else:
            # lsof found nothing (missing lsof, or non-LISTEN socket): fall back
            # to asking the user instead of blindly proceeding.
            raise ValueError(
                f"Port {port} is already in use but no owning PID was found (is lsof installed?); "
                "stop that service manually"
            ) from None
        if _port_in_use(port):
            raise ValueError(f"Port {port} is still in use after --force; stop that service manually") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Guildhall frontend + backend without opening a browser")
    parser.add_argument("--port", type=int, default=5173, help="Frontend port (default: 5173)")
    parser.add_argument("--force", action="store_true", help="Kill processes occupying the frontend/backend ports before starting")
    args = parser.parse_args()
    from guildhall.config import load

    backend_port = load().server_port
    check_ports([backend_port, args.port], force=args.force)
    print("[dev] Installing locked frontend dependencies…", flush=True)
    subprocess.run(["pnpm", "install", "--frozen-lockfile"], cwd=ROOT / "frontend", check=True)
    env = os.environ.copy()
    env["GUILDHALL_API_URL"] = f"http://127.0.0.1:{backend_port}"
    env["VITE_GUILDHALL_DEMO"] = "0"
    env["BROWSER"] = "none"
    print(f"[dev] Starting backend: http://127.0.0.1:{backend_port}", flush=True)
    print(f"[dev] Starting frontend: http://127.0.0.1:{args.port} (no browser launch)", flush=True)
    print("[dev] Ctrl+C stops both services. Restart this command after saving model settings.", flush=True)
    return supervise([
        ([sys.executable, "-m", "guildhall.main"], ROOT / "backend"),
        (["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", str(args.port), "--strictPort"], ROOT / "frontend"),
    ], env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"[dev] {exc}", file=sys.stderr)
        raise SystemExit(1)

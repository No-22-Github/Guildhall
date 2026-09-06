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


def stop(children: list[subprocess.Popen]) -> None:
    # A wrapper may exit before its children, so signal its group even if it exited.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def supervise(commands: list[tuple[list[str], Path]], env: dict[str, str]) -> int:
    children: list[subprocess.Popen] = []
    interrupted = False

    def shutdown(signum, frame):
        nonlocal interrupted
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


def check_ports(ports: list[int]) -> None:
    if len(set(ports)) != len(ports):
        raise ValueError("Frontend and backend must use different ports")
    for port in ports:
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port: {port}")
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError(f"Port {port} is already in use; stop that service or choose another port") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Guildhall frontend + backend without opening a browser")
    parser.add_argument("--port", type=int, default=5173, help="Frontend port (default: 5173)")
    args = parser.parse_args()
    from guildhall.config import load

    backend_port = load().server_port
    check_ports([backend_port, args.port])
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

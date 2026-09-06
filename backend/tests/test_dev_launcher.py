"""Exercise real subprocess shutdown without starting models or the user backend."""
import importlib.util
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'dev.py'
spec = importlib.util.spec_from_file_location('dev_launcher', SCRIPT)
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


def test_ports_reject_conflict_and_collision():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        with pytest.raises(ValueError, match='already in use'):
            dev.check_ports([port])
    with pytest.raises(ValueError, match='different ports'):
        dev.check_ports([5173, 5173])


def test_peer_failure_stops_real_child(tmp_path):
    marker = tmp_path / 'stopped'
    worker = "import signal,time,pathlib; signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path('stopped').write_text('yes'), exit(0))); time.sleep(30)"
    result = dev.supervise([
        ([sys.executable, '-c', worker], tmp_path),
        ([sys.executable, '-c', 'import time; time.sleep(.4); exit(7)'], tmp_path),
    ], os.environ.copy())
    assert result == 7
    assert marker.read_text() == 'yes'


def test_interrupt_stops_both_process_groups(tmp_path):
    runner = tmp_path / 'runner.py'
    runner.write_text(f'''import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('dev', {str(SCRIPT)!r})
dev = importlib.util.module_from_spec(spec); spec.loader.exec_module(dev)
worker = "import signal,time,pathlib; pathlib.Path('ready-NAME').write_text('yes'); signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path('stop-NAME').write_text('yes'), exit(0))); time.sleep(30)"
sys.exit(dev.supervise([([sys.executable, '-c', worker.replace('NAME', name)], Path.cwd()) for name in ('a','b')], os.environ.copy()))
''')
    parent = subprocess.Popen([sys.executable, str(runner)], cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / f'ready-{name}').exists() for name in ('a', 'b')):
            assert time.monotonic() < deadline
            time.sleep(.03)
        parent.send_signal(signal.SIGINT)
        assert parent.wait(timeout=7) == 0
        assert all((tmp_path / f'stop-{name}').read_text() == 'yes' for name in ('a', 'b'))
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=7)

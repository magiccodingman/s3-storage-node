"""Bounded local diagnostics: never read the remote filesystem or credentials."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .logging import event
from .render import write_atomic


def capture(directory: Path, generation: int, phase: str) -> None:
    def collect() -> None:
        try:
            tasks = []
            for status in Path('/proc').glob('[0-9]*/task/[0-9]*/status'):
                if len(tasks) >= 64:
                    break
                try:
                    lines = status.read_text().splitlines()
                    state = next((s for s in lines if s.startswith('State:')), '')
                    if '\tD ' not in state and '\tX ' not in state:
                        continue
                    task = status.parent
                    tasks.append({'pid': int(task.parent.parent.name), 'tid': int(task.name),
                                  'state': state, 'stack': (task / 'stack').read_text()[:8192]})
                except (OSError, ValueError):
                    continue
            cifs = []
            try:
                cifs = [s for s in Path('/proc/fs/cifs/DebugData').read_text().splitlines()
                        if any(marker in s for marker in ('Number of credits:', 'TCP status:', 'Dialect', 'Allocated channels:'))][:64]
            except OSError:
                pass
            result = {'at': time.time(), 'generation': generation, 'phase': phase,
                      'kernel': Path('/proc/sys/kernel/osrelease').read_text().strip(),
                      'blocked_tasks': tasks, 'cifs_connection_counters': cifs}
            write_atomic(directory / f'{generation}-{phase}.json', json.dumps(result, indent=2) + '\n')
        except Exception as exc:
            event('warning', 'recovery_diagnostics_failed', error=str(exc))
    worker = threading.Thread(target=collect, name='recovery-diagnostics', daemon=True)
    worker.start()
    worker.join(timeout=2)
    if worker.is_alive():
        event('warning', 'recovery_diagnostics_timeout', generation=generation, phase=phase)

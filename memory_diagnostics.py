"""Bounded numeric memory diagnostics. Never records request data or locals."""
import collections
import gc
import os
from pathlib import Path
import sqlite3
import ssl
import threading
import time
import tracemalloc

_lock = threading.Lock()
_history = collections.deque(maxlen=12)
_jobs = {}
_started = False
_previous = None

def _read_pairs(path):
    try:
        pairs = {}
        for line in Path(path).read_text().splitlines():
            parts = line.replace(':', '').split()
            if len(parts) > 1 and parts[1].isdigit():
                pairs[parts[0]] = int(parts[1])
        return pairs
    except OSError:
        return {}

def measure():
    proc = _read_pairs('/proc/self/status')
    cgroup = _read_pairs('/sys/fs/cgroup/memory.stat')
    current, peak = tracemalloc.get_traced_memory()
    return {'rss_bytes': proc.get('VmRSS', 0)*1024,
            'rss_peak_bytes': proc.get('VmHWM', 0)*1024,
            'threads': proc.get('Threads', 0),
            'python_bytes': current, 'python_peak_bytes': peak,
            'tracer_bytes': tracemalloc.get_tracemalloc_memory(),
            'cgroup_anon_bytes': cgroup.get('anon', 0),
            'cgroup_file_bytes': cgroup.get('file', 0),
            'cgroup_kernel_bytes': cgroup.get('kernel', 0)}

def observe(name, function, *args, **kwargs):
    before = measure()
    started = time.monotonic()
    try:
        return function(*args, **kwargs)
    finally:
        after = measure()
        with _lock:
            item = _jobs.setdefault(name, {'calls': 0, 'rss_delta_sum': 0,
                'python_delta_sum': 0, 'rss_delta_max': 0, 'last_seconds': 0})
            item['calls'] += 1
            delta = after['rss_bytes']-before['rss_bytes']
            item['rss_delta_sum'] += delta
            item['python_delta_sum'] += after['python_bytes']-before['python_bytes']
            item['rss_delta_max'] = max(item['rss_delta_max'], delta)
            item['last_seconds'] = round(time.monotonic()-started, 3)

def sample():
    global _previous
    metrics = measure()
    # Cap profiler overhead; continue numeric RSS/cgroup observation if exceeded.
    if metrics['tracer_bytes'] > 16*1024*1024:
        tracemalloc.stop()
        _previous = None
        metrics['tracing_stopped_for_overhead'] = True
    growth = []
    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot()
        if _previous is not None:
            for change in snap.compare_to(_previous, 'lineno'):
                frame = change.traceback[0]
                if change.size_diff > 0 and Path(frame.filename).name != 'memory_diagnostics.py':
                    growth.append({'file': Path(frame.filename).name, 'line': frame.lineno,
                                   'bytes': change.size_diff, 'count': change.count_diff})
                if len(growth) == 8:
                    break
        _previous = snap
    counts = collections.Counter()
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection): counts['sqlite_connections'] += 1
        elif isinstance(obj, sqlite3.Cursor): counts['sqlite_cursors'] += 1
        elif isinstance(obj, ssl.SSLContext): counts['ssl_contexts'] += 1
    with _lock:
        entry = {'at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                 **metrics, 'objects': dict(counts), 'growth': growth,
                 'jobs': {k: dict(v) for k,v in _jobs.items()}}
        _history.append(entry)
    print('memory_diagnostics rss_bytes=%s python_bytes=%s cgroup_anon=%s cgroup_file=%s' %
          (metrics['rss_bytes'], metrics['python_bytes'], metrics['cgroup_anon_bytes'], metrics['cgroup_file_bytes']), flush=True)
    return entry

def status():
    with _lock:
        return {'enabled': _started, 'current': measure(), 'samples': list(_history)}

def _loop():
    while True:
        try: sample()
        except Exception as exc: print('memory_diagnostics error='+type(exc).__name__, flush=True)
        time.sleep(120)

def start(app):
    global _started
    if _started: return
    _started = True
    tracemalloc.start(3)
    # Wrap the existing work units, retaining their arguments, return values and errors.
    targets = [(app.guest_service, 'drain_stock'), (app.guest_service, 'sync_requests'),
        (app.guest_service, 'plan'), (app.guest_service, 'deliver'),
        (app.concierge, 'deliver'), (app.line_reservations, 'drain'),
        (app.calendar_sync, 'reconcile'), (app.calendar_sync.Google, 'check_calendar'),
        (app, 'reconcile_reservations'), (app, 'expire_public_reservations'),
        (app.refunds, 'process'), (app.line_receipts, 'drain'),
        (app.line_receipts, 'drain_reviews'), (app.supplies, 'tick'),
        (app.line_delivery, 'deliver'), (app.marketing, 'archive_due')]
    for owner, name in targets:
        original = getattr(owner, name)
        label = getattr(owner, '__name__', owner.__class__.__name__)+'.'+name
        def wrapped(*args, _name=label, _fn=original, **kwargs):
            return observe(_name, _fn, *args, **kwargs)
        setattr(owner, name, wrapped)
    threading.Thread(target=_loop, name='memory-diagnostics', daemon=True).start()

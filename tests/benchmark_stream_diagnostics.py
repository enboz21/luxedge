"""Compare an unmodified Git revision with the working tree at the real frame-worker entry point."""
import argparse
import io
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def measure(source, profile, frames, target_fps):
    os.environ['APPDATA'] = str(profile)
    sys.path.insert(0, str(source))
    import ambilight_pc as backend
    import numpy as np

    width, height = 1920, 1080
    pixels = np.random.default_rng(42).integers(0, 256, (height, width, 4), dtype=np.uint8).tobytes()

    class Capture:
        monitors = [{}, {'left': 0, 'top': 0, 'width': width, 'height': height}]

        def grab(self, monitor):
            return SimpleNamespace(raw=bytearray(pixels), width=width, height=height)

        def close(self):
            pass

    frame_times = []
    completions = []
    sent = 0
    cfg = {'wemos_ip': '192.0.2.1', 'wemos_port': 7777, 'fps': target_fps, 'led_monitor_index': 1,
           'top_leds': 34, 'bottom_leds': 0, 'left_leds': 20, 'right_leds': 20,
           'edge_width': 20, 'edge_offset': 0}
    frame_delay = backend.frame_delay

    def send(payload, address):
        nonlocal sent
        if address != ('192.0.2.1', 7777) or len(payload) != 222:
            raise AssertionError((address, len(payload)))
        sent += 1
        if sent == frames:
            backend.running = False
        return len(payload)

    def observe_frame(start, finished, interval):
        frame_times.append((finished - start) * 1000)
        completions.append(finished)
        return frame_delay(start, finished, interval)

    udp = SimpleNamespace(sendto=send, close=lambda: None)
    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(io.StringIO()))
        for name, value in {
            'running': True, 'load_config': lambda: cfg, 'get_local_ip': lambda: None, 'mss': Capture,
            'get_led_monitor_index': lambda capture: 1, 'frame_delay': observe_frame,
            'socket': SimpleNamespace(socket=lambda *args: udp, AF_INET=2, SOCK_DGRAM=2),
            'wemos_connectivity_checker': lambda *args: None,
        }.items():
            stack.enter_context(mock.patch.object(backend, name, value))
        current = hasattr(backend.log_history, 'start_stream_writer')
        if current:
            backend.log_history.start_stream_writer()
            stack.callback(backend.log_history.stop_stream_writer)
        worker = backend.threading.Thread(target=backend.ambilight_worker, args=(cfg,),
                                          name=getattr(backend, 'STREAM_THREAD', 'ambilight-stream'))
        worker.start()
        worker.join(timeout=frames / target_fps + 15)
        if worker.is_alive():
            backend.running = False
            worker.join()
            raise RuntimeError('Frame benchmark timed out')
    if len(frame_times) != frames:
        raise RuntimeError(f'Expected {frames} completed frames, observed {len(frame_times)}')
    rows = backend.log_history.snapshot()['logs']
    samples = sum(row['code'] == 'udp_send_sample' for row in rows)
    if current and not samples:
        raise RuntimeError('Stream diagnostics did not produce samples')
    return {
        'fps': round((frames - 1) / (completions[-1] - completions[0]), 2),
        'p95_frame_ms': round(sorted(frame_times)[math.ceil(0.95 * frames) - 1], 3),
        'log_rows': len(rows),
        'udp_samples': samples,
        'numpy': np.__version__,
    }


def benchmark(revision, pairs, frames, target_fps):
    build = ROOT / 'build' / 'tests'
    build.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='stream-benchmark-', dir=build) as work:
        work = Path(work)
        baseline = work / 'baseline'
        baseline.mkdir()
        names = subprocess.check_output(['git', 'ls-tree', '--name-only', revision], cwd=ROOT, text=True)
        for name in names.splitlines():
            if name.endswith('.py'):
                content = subprocess.check_output(['git', 'show', f'{revision}:{name}'], cwd=ROOT)
                (baseline / name).write_bytes(content)
        results = {'baseline': [], 'enabled': []}
        for index in range(pairs):
            runs = [('baseline', baseline), ('enabled', ROOT)]
            if index % 2:
                runs.reverse()
            for name, source in runs:
                profile = work / f'{name}-{index}'
                profile.mkdir()
                command = [sys.executable, '-B', __file__, '--sample', str(source), str(profile),
                           '--frames', str(frames), '--fps', str(target_fps)]
                raw = subprocess.check_output(command, cwd=ROOT, text=True, timeout=frames / target_fps + 30,
                                              encoding='utf-8')
                results[name].append(json.loads(raw))
        medians = {name: {metric: statistics.median(row[metric] for row in rows)
                          for metric in ('fps', 'p95_frame_ms')} for name, rows in results.items()}
        print(json.dumps({'revision': revision, 'pairs': pairs, 'frames_per_run': frames, 'target_fps': target_fps,
                          'windows': platform.platform(), 'python': platform.python_version(),
                          'runs': results, 'medians': medians}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='HEAD', help='Git revision for the old frame worker')
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--sample', nargs=2, type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.pairs < 1 or args.frames < 2 or args.fps < 1:
        parser.error('Use positive pairs and FPS, and at least two frames')
    if args.sample:
        print(json.dumps(measure(*args.sample, args.frames, args.fps)))
    else:
        benchmark(args.baseline, args.pairs, args.frames, args.fps)

"""Offline regression checks for engine/runtime_metrics.py.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s tests \
    -p 'test_engine_rw_metrics.py' -v

Fixtures stay under data/temp. No live worker is sampled, no global tracing is
started and gc is never forced; /proc, tracemalloc and the engine registries
are mocked.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import runtime_metrics as metrics_module
from engine.runtime_metrics import WorkerMetrics, process_memory_kib

MIB = 1024 * 1024


def temporary_workspace(test: unittest.TestCase) -> Path:
    base = ROOT / 'data/temp'
    base.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='engine-rw-metrics-', dir=base)
    test.addCleanup(temporary.cleanup)
    return Path(temporary.name)


def config(**overrides) -> dict:
    """Nested runtime config shaped like data/config.yaml."""
    return {'engine': {'memory_metrics': dict(overrides)}}


def fake_engine_modules(*, owners=None, hooks=None, sections=None, async_tools=None,
                        counts_error=None) -> types.SimpleNamespace:
    """Build importable stand-ins for the three registries sample() imports."""
    owners = {'plugin_a': 2, 'plugin_b': 1} if owners is None else owners
    hooks = {'before_step': [object(), object()], 'after_step': [object()]} if hooks is None else hooks
    sections = {'runtime': object(), 'workspace': object(), 'tools': object()} if sections is None else sections
    async_tools = ({'a1': {'status': 'running'}, 'a2': {'status': 'running'},
                    'a3': {'status': 'completed'}, 'a4': {}}
                   if async_tools is None else async_tools)

    def counts():
        if counts_error is not None:
            raise counts_error
        return dict(owners)

    hook_registry = types.SimpleNamespace(
        _ledger=types.SimpleNamespace(counts=counts),
        _hooks=hooks,
    )
    prompt_section_registry = types.SimpleNamespace(_sections=sections)
    hooks_module = types.ModuleType('engine.hooks')
    hooks_module.hook_registry = hook_registry
    sections_module = types.ModuleType('engine.faces.prompt_sections')
    sections_module.prompt_section_registry = prompt_section_registry
    async_tools_module = types.ModuleType('engine.inference.async_tools')
    async_tools_module._async_tool_tasks = async_tools
    return types.SimpleNamespace(
        modules={
            'engine.hooks': hooks_module,
            'engine.faces.prompt_sections': sections_module,
            'engine.inference.async_tools': async_tools_module,
        },
        hook_registry=hook_registry,
        prompt_section_registry=prompt_section_registry,
        async_tool_tasks=async_tools,
    )


class FakeTools:
    def __init__(self, count: int = 4) -> None:
        self.count = count

    def list_specs(self) -> list[str]:
        return [f'tool_{index}' for index in range(self.count)]


class FakeTask:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class WorkerMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary_workspace(self)

    # --- helpers ---------------------------------------------------------
    def diagnostics(self, root: Path | None = None) -> Path:
        return (root or self.root) / 'data' / 'diagnostics'

    def make(self, cfg: dict | None = None, *, worker_id: str = 'agent:1',
             generation_id: str = 'gen-1', root: Path | None = None) -> WorkerMetrics:
        instance = WorkerMetrics(root or self.root, worker_id, generation_id,
                                 config() if cfg is None else cfg)
        self.addCleanup(instance.close)
        return instance

    def rows(self, root: Path | None = None) -> list[dict]:
        directory = self.diagnostics(root)
        if not directory.is_dir():
            return []
        rows: list[dict] = []
        for path in sorted(directory.glob('memory-*.jsonl')):
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def run_sample(self, instance: WorkerMetrics, active=(), tools=None) -> None:
        async def go() -> None:
            instance.sample(set(active), tools if tools is not None else FakeTools())

        asyncio.run(go())

    def sample_env(self, **kwargs):
        env = fake_engine_modules(**kwargs)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(sys.modules, env.modules))
        stack.enter_context(patch.object(metrics_module, 'process_memory_kib',
                                         return_value={'rss_kib': 4096, 'swap_kib': 16,
                                                       'rss_swap_kib': 4112, 'threads': 5,
                                                       'file_descriptors': 9}))
        return stack, env

    # --- defaults / disabled --------------------------------------------
    def test_defaults_enabled_with_production_bounds(self):
        instance = self.make(worker_id='agent:7/sub')
        self.assertTrue(self.diagnostics().is_dir())
        path = self.diagnostics() / 'memory-agent_7_sub.jsonl'
        self.assertTrue(path.is_file())
        self.assertEqual(instance.interval, 60.0)
        self.assertEqual(type(instance.handler).__name__, 'RotatingFileHandler')
        self.assertEqual(instance.handler.maxBytes, 5 * 1024 * 1024)
        self.assertEqual(instance.handler.backupCount, 1)
        self.assertEqual(instance.handler.encoding, 'utf-8')
        self.assertFalse(instance.sink.propagate)
        self.assertEqual(instance.handler.formatter._fmt, '%(message)s')
        self.assertFalse(instance.owns_trace)
        self.assertEqual((instance.claimed, instance.reaped), (0, 0))

    def test_disabled_creates_no_diagnostics_directory(self):
        instance = self.make(config(enabled=False))
        self.assertIsNone(instance.handler)
        self.assertIsNone(instance.sink)
        self.assertFalse(instance.owns_trace)
        self.assertFalse((self.root / 'data').exists())
        self.run_sample(instance, active=[FakeTask(False)])
        self.assertFalse((self.root / 'data').exists())

    def test_disabled_via_string_false_also_stays_off(self):
        instance = self.make(config(enabled='false'))
        self.assertIsNone(instance.sink)
        self.assertFalse((self.root / 'data').exists())

    def test_interval_is_clamped_and_defaults_on_bad_input(self):
        for value, expected in ((1, 10.0), (3599.9, 3599.9), (999999, 3600.0), ('oops', 60.0)):
            with self.subTest(value=value):
                self.assertEqual(self.make(config(interval_sec=value)).interval, expected)

    def test_worker_id_is_sanitized_and_truncated(self):
        instance = self.make(worker_id='a' * 100)
        self.assertEqual((self.diagnostics() / ('memory-' + 'a' * 64 + '.jsonl')).is_file(), True)
        empty = self.make(worker_id='')
        self.assertTrue((self.diagnostics() / 'memory-worker.jsonl').is_file())
        self.assertIsNotNone(empty.handler)

    # --- sampling --------------------------------------------------------
    def test_sample_is_throttled_to_one_row_per_interval(self):
        clock = types.SimpleNamespace(monotonic=lambda: 1000.0)
        stack, _ = self.sample_env()
        with stack, patch.object(metrics_module, 'time', clock):
            instance = self.make()
            self.run_sample(instance, active=[FakeTask(False)])
            self.assertEqual(len(self.rows()), 1)
            self.assertEqual(instance.next_sample, 1060.0)
            clock.monotonic = lambda: 1059.9
            self.run_sample(instance, active=[FakeTask(False)])
            self.assertEqual(len(self.rows()), 1)
            clock.monotonic = lambda: 1060.0
            self.run_sample(instance, active=[FakeTask(False)])
            self.assertEqual(len(self.rows()), 2)
            self.assertEqual(instance.next_sample, 1120.0)

    def test_sample_records_counters_and_active_task_totals(self):
        stack, env = self.sample_env()
        with stack:
            instance = self.make()
            instance.claimed, instance.reaped = 37, 35
            self.run_sample(instance, active=[FakeTask(False), FakeTask(False), FakeTask(True)])
        row = self.rows()[0]
        self.assertEqual(row['worker_id'], 'agent:1')
        self.assertEqual(row['generation_id'], 'gen-1')
        self.assertEqual((row['tasks_claimed'], row['tasks_reaped']), (37, 35))
        self.assertEqual(row['active_tasks'], 2)
        self.assertEqual(row['async_tools'], {'running': 2, 'completed': 1, 'unknown': 1})
        self.assertEqual(row['plugin_disposers'], {'plugin_a': 2, 'plugin_b': 1})
        self.assertEqual(row['plugin_disposers_total'], 3)
        self.assertEqual(row['hooks'], 3)
        self.assertEqual(row['prompt_sections'], len(env.prompt_section_registry._sections))
        self.assertEqual(row['registered_tools'], 4)
        self.assertEqual(row['pid'], os.getpid())
        self.assertGreaterEqual(row['asyncio_tasks'], 1)
        self.assertIsInstance(row['gc_counts'], list)
        self.assertIsInstance(row['gc_collections'], list)
        self.assertEqual(row['rss_kib'], 4096)

    def test_rows_carry_metadata_only_and_are_single_line_json(self):
        stack, _ = self.sample_env()
        with stack:
            instance = self.make()
            self.run_sample(instance, active=[FakeTask(False)])
        line = (self.diagnostics() / 'memory-agent_1.jsonl').read_text(encoding='utf-8')
        self.assertTrue(line.endswith('\n'))
        self.assertEqual(line.count('\n'), 1)
        self.assertNotIn('": "', line)  # compact separators
        row = json.loads(line)
        self.assertEqual(set(row), {
            'ts', 'pid', 'worker_id', 'generation_id', 'uptime_sec',
            'rss_kib', 'swap_kib', 'rss_swap_kib', 'threads', 'file_descriptors',
            'tasks_claimed', 'tasks_reaped', 'active_tasks', 'asyncio_tasks',
            'async_tools', 'plugin_disposers', 'plugin_disposers_total', 'hooks',
            'prompt_sections', 'registered_tools', 'python_allocated_blocks',
            'gc_counts', 'gc_collections',
        })
        for forbidden in ('content', 'text', 'messages', 'prompt_text', 'body',
                          'tool_result', 'task', 'secret'):
            self.assertNotIn(forbidden, row)
        self.assertIsInstance(row['python_allocated_blocks'], int)
        self.assertNotIn('\n', row['ts'])

    def test_sleeping_sample_still_throttles_after_trace_values(self):
        """tracemalloc values are re-read only when this instance owns tracing."""
        fake = types.ModuleType('tracemalloc')
        fake.is_tracing = Mock(return_value=False)
        fake.start = Mock()
        fake.stop = Mock()
        fake.get_traced_memory = Mock(return_value=(11, 22))
        stack, _ = self.sample_env()
        with stack, patch.dict(sys.modules, {'tracemalloc': fake}):
            instance = self.make(config(tracemalloc=True))
            self.run_sample(instance, active=[FakeTask(False)])
            instance.close()
        row = self.rows()[0]
        self.assertEqual((row['python_traced_bytes'], row['python_traced_peak_bytes']), (11, 22))

    # --- /proc fixtures --------------------------------------------------
    def proc_fixture(self, *, status=True, smaps=True, fds=3) -> Path:
        directory = self.root / 'proc'
        directory.mkdir(parents=True, exist_ok=True)
        if status:
            (directory / 'status').write_text(
                'Name:\tpython3\n'
                'VmPeak:\t   99999 kB\n'
                'VmRSS:\t    4096 kB\n'
                'VmSwap:\t      16 kB\n'
                'Threads:\t       5\n'
                'SigQ:\t 0/1024\n', encoding='utf-8')
        if smaps:
            (directory / 'smaps_rollup').write_text(
                '00400000-7ffffffff000 ---p 00000000 00:00 0 [rollup]\n'
                'Rss:               4096 kB\n'
                'Pss:               2048 kB\n'
                'Private_Clean:      512 kB\n'
                'Private_Dirty:     1024 kB\n'
                'Swap:                16 kB\n'
                'Pss_Anon:          1536 kB\n', encoding='utf-8')
        fd_dir = directory / 'fd'
        fd_dir.mkdir(exist_ok=True)
        for index in range(fds):
            (fd_dir / str(index)).write_text('', encoding='utf-8')
        return directory

    def test_proc_status_and_smaps_are_merged(self):
        sample = process_memory_kib(self.proc_fixture())
        self.assertEqual(sample, {
            'rss_kib': 4096,
            'swap_kib': 16,
            'threads': 5,
            'pss_kib': 2048,
            'uss_kib': 1536,
            'rss_swap_kib': 4112,
            'file_descriptors': 3,
        })

    def test_proc_without_smaps_falls_back_to_status_only(self):
        sample = process_memory_kib(self.proc_fixture(smaps=False, fds=0))
        self.assertEqual(sample, {'rss_kib': 4096, 'swap_kib': 16, 'threads': 5,
                                  'rss_swap_kib': 4112, 'file_descriptors': 0})

    def test_proc_without_fd_directory_is_partial(self):
        directory = self.proc_fixture(fds=0)
        (directory / 'fd').rmdir()
        sample = process_memory_kib(directory)
        self.assertNotIn('file_descriptors', sample)
        self.assertEqual(sample['rss_kib'], 4096)

    def test_missing_proc_directory_returns_empty_mapping(self):
        self.assertEqual(process_memory_kib(self.root / 'no-such-proc'), {})

    def test_malformed_proc_values_are_ignored(self):
        directory = self.root / 'bad-proc'
        directory.mkdir()
        (directory / 'status').write_text('VmRSS:\tnot-a-number\nThreads:\t1\n', encoding='utf-8')
        (directory / 'smaps_rollup').write_text('Rss:\tbroken\n', encoding='utf-8')
        self.assertEqual(process_memory_kib(directory), {})

    def test_default_proc_path_is_self_without_touching_other_processes(self):
        self.assertEqual(process_memory_kib.__defaults__, (Path('/proc/self'),))

    # --- rotation / close ------------------------------------------------
    def test_log_is_bounded_to_five_mib_and_one_backup(self):
        instance = self.make(worker_id='rot')
        self.assertEqual(instance.handler.maxBytes, 5 * MIB)
        self.assertEqual(instance.handler.backupCount, 1)
        chunk = json.dumps({'pad': 'x' * (MIB - 32)}) + '\n'
        for _ in range(12):
            instance.sink.info(chunk)
        instance.handler.flush()
        files = sorted(path.name for path in self.diagnostics().glob('memory-rot*'))
        self.assertEqual(files, ['memory-rot.jsonl', 'memory-rot.jsonl.1'])
        for path in self.diagnostics().glob('memory-rot*'):
            self.assertLessEqual(path.stat().st_size, 5 * MIB)

    def test_close_is_repeatable_and_disables_sampling(self):
        stack, _ = self.sample_env()
        with stack:
            instance = self.make()
            self.run_sample(instance, active=[FakeTask(False)])
            instance.close()
            self.assertIsNone(instance.handler)
            self.assertIsNone(instance.sink)
            self.assertFalse(instance.owns_trace)
            instance.close()
            instance.close()
            self.run_sample(instance, active=[FakeTask(False)])
        self.assertEqual(len(self.rows()), 1)

    def test_close_without_handler_is_noop(self):
        instance = self.make(config(enabled=False))
        instance.close()
        instance.close()
        self.assertIsNone(instance.handler)

    # --- tracemalloc ownership (mocked) ----------------------------------
    def tracemalloc_stub(self, *, tracing: bool) -> types.ModuleType:
        stub = types.ModuleType('tracemalloc')
        stub.is_tracing = Mock(return_value=tracing)
        stub.start = Mock()
        stub.stop = Mock()
        stub.get_traced_memory = Mock(return_value=(0, 0))
        return stub

    def test_tracemalloc_disabled_by_default(self):
        stub = self.tracemalloc_stub(tracing=False)
        with patch.dict(sys.modules, {'tracemalloc': stub}):
            instance = self.make()
            instance.close()
        stub.is_tracing.assert_not_called()
        stub.start.assert_not_called()
        stub.stop.assert_not_called()
        self.assertFalse(instance.owns_trace)

    def test_tracemalloc_starts_and_stops_only_own_trace(self):
        stub = self.tracemalloc_stub(tracing=False)
        with patch.dict(sys.modules, {'tracemalloc': stub}):
            instance = self.make(config(tracemalloc=True))
            self.assertTrue(instance.owns_trace)
            stub.start.assert_called_once_with(5)
            instance.close()
            self.assertFalse(instance.owns_trace)
            stub.stop.assert_called_once_with()
            instance.close()
            stub.stop.assert_called_once_with()

    def test_tracemalloc_never_stops_a_trace_it_did_not_start(self):
        stub = self.tracemalloc_stub(tracing=True)
        with patch.dict(sys.modules, {'tracemalloc': stub}):
            instance = self.make(config(tracemalloc=True))
            self.assertFalse(instance.owns_trace)
            stub.start.assert_not_called()
            instance.close()
        stub.stop.assert_not_called()

    # --- failure paths stay non-fatal ------------------------------------
    def test_initialization_failure_is_non_fatal(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('fixture denial')), \
                self.assertLogs(level=logging.WARNING) as captured:
            instance = self.make()
        self.assertIn('Worker memory diagnostics initialization failed', captured.output[0])
        self.assertIsNone(instance.handler)
        self.assertIsNone(instance.sink)
        self.assertFalse((self.root / 'data').exists())
        self.run_sample(instance, active=[FakeTask(False)])  # no sink, no raise

    def test_sample_failure_is_non_fatal_and_writes_nothing(self):
        stack, _ = self.sample_env(counts_error=RuntimeError('fixture denial'))
        with stack:
            instance = self.make()
            with self.assertLogs(level=logging.WARNING) as captured:
                self.run_sample(instance, active=[FakeTask(False)])
        self.assertIn('Worker memory sample failed', captured.output[0])
        self.assertTrue((self.diagnostics() / 'memory-agent_1.jsonl').is_file())
        self.assertEqual(self.rows(), [])
        self.assertEqual(instance.next_sample > 0, True)

    def test_missing_engine_registry_modules_are_non_fatal(self):
        instance = self.make()
        with patch.dict(sys.modules, {'engine.hooks': None}):
            with self.assertLogs(level=logging.WARNING) as captured:
                self.run_sample(instance, active=[FakeTask(False)])
        self.assertIn('Worker memory sample failed', captured.output[0])
        self.assertEqual(self.rows(), [])

    def test_sample_does_not_force_gc(self):
        stack, _ = self.sample_env()
        with stack, patch.object(metrics_module.gc, 'collect',
                                 side_effect=AssertionError('gc must not be forced')) as collect:
            instance = self.make()
            self.run_sample(instance, active=[FakeTask(False)])
        collect.assert_not_called()
        self.assertEqual(len(self.rows()), 1)


if __name__ == '__main__':
    unittest.main()

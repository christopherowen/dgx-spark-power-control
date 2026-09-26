# SPDX-License-Identifier: GPL-2.0-only
"""Trace lifecycle checks using temporary files only; never live tracefs."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "diagnostics/ffa-trace/collect.py"
SPEC = importlib.util.spec_from_file_location("ffa_trace", SOURCE)
TRACE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRACE)


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "tracefs"
        self.root.mkdir()
        (self.root / "instances").mkdir()
        for name, value in {"available_filter_functions": "\n".join(TRACE.FUNCTIONS) + "\nunrelated",
                            "available_tracers": "nop function_graph", "current_tracer": "nop",
                            "tracing_on": "1"}.items():
            (self.root / name).write_text(value)
        self.output = self.base / "capture"
        self.writes = []
        self.missing = None
        self.fail_on = None
        original_mkdir, original_rmdir, original_write = Path.mkdir, Path.rmdir, TRACE.write

        def mkdir(path, *args, **kwargs):
            original_mkdir(path, *args, **kwargs)
            if path.parent == self.root / "instances":
                for name in ("tracing_on", "current_tracer", "set_ftrace_filter",
                             "buffer_size_kb", "buffer_total_size_kb", "trace_clock", "trace"):
                    if name != self.missing:
                        (path / name).write_text("[local] mono" if name == "trace_clock" else "0")
                (path / "trace").write_text("# simulated trace\nffa_sync_send_receive2();\n")
                (path / "options").mkdir()
                for name in TRACE.OPTIONS:
                    (path / "options" / name).write_text("0")
                stats = path / "per_cpu/cpu0"
                stats.mkdir(parents=True)
                (stats / "stats").write_text("overrun: 0\n")

        def rmdir(path):
            if path.parent == self.root / "instances":
                shutil.rmtree(path)  # tracefs removes virtual files with its instance.
            else:
                original_rmdir(path)

        def write(path, value):
            self.writes.append((path, value))
            if path.name == self.fail_on:
                self.fail_on = None
                raise OSError("simulated control failure")
            original_write(path, value)

        for patcher in (patch.object(Path, "mkdir", mkdir), patch.object(Path, "rmdir", rmdir),
                        patch.object(TRACE, "write", write)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_passive_capture_is_filtered_and_instance_local(self):
        report = TRACE.capture(self.output, 1, root=self.root, sleep=lambda _: None)
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["cleanup_complete"])
        self.assertTrue(report["global_controls_unchanged"])
        self.assertEqual(report["collector_firmware_requests"], 0)
        self.assertEqual(list((self.root / "instances").iterdir()), [])
        self.assertTrue(all(p.is_relative_to(self.root / "instances") for p, _ in self.writes))
        names = [(p.name, value) for p, value in self.writes]
        filter_at = next(i for i, x in enumerate(names) if x[0] == "set_ftrace_filter")
        start_at = names.index(("tracing_on", "1"))
        self.assertLess(filter_at, start_at)
        self.assertEqual(set(names[filter_at][1].splitlines()), set(TRACE.FUNCTIONS))
        self.assertIn("ffa_sync", (self.output / "trace.txt").read_text())

    def test_interrupt_stops_tracing_saves_partial_capture_and_cleans_up(self):
        def stop(_):
            raise KeyboardInterrupt
        report = TRACE.capture(self.output, 1, root=self.root, sleep=stop)
        self.assertEqual(report["status"], "interrupted")
        self.assertTrue(report["cleanup_complete"])
        self.assertTrue((self.output / "trace.txt").exists())
        self.assertEqual(list((self.root / "instances").iterdir()), [])

    def test_configuration_failure_has_no_global_fallback(self):
        for missing, fail_on in (("set_ftrace_filter", None), (None, "current_tracer")):
            self.missing, self.fail_on = missing, fail_on
            output = self.base / f"failed-{missing}-{fail_on}"
            with self.assertRaises(RuntimeError):
                TRACE.capture(output, 1, root=self.root, sleep=lambda _: self.fail("must not capture"))
            report = json.loads((output / "metadata.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["cleanup_complete"])
            self.assertTrue(report["global_controls_unchanged"])
            self.assertEqual(list((self.root / "instances").iterdir()), [])

    def test_no_functions_or_existing_output_refused_without_tracing(self):
        (self.root / "available_filter_functions").write_text("unrelated\n")
        with self.assertRaises(RuntimeError):
            TRACE.capture(self.output, 1, root=self.root)
        self.assertFalse(self.output.exists())
        (self.root / "available_filter_functions").write_text(TRACE.FUNCTIONS[0])
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            TRACE.capture(self.output, 1, root=self.root)
        self.assertEqual(self.writes, [])

    def test_duration_and_export_are_bounded(self):
        for value in ("nan", "inf", "0", "31", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                TRACE.bounded_duration(value)
        with patch.object(TRACE, "MAX_TRACE_BYTES", 8):
            report = TRACE.capture(self.output, 1, root=self.root, sleep=lambda _: None)
        self.assertTrue(report["trace_truncated"])
        self.assertEqual((self.output / "trace.txt").stat().st_size, 8)


if __name__ == "__main__":
    unittest.main()

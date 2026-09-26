#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bounded passive Linux FF-A call tracing in a private ftrace instance.

Records existing calls only. Does not send FF-A requests, load drivers, read
secure memory, consume EC registers, or enable global tracing settings.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import signal
import sys
import time
import uuid

TRACE_ROOT = Path("/sys/kernel/tracing")
FUNCTIONS = ("ffa_sync_send_receive", "ffa_sync_send_receive2",
             "ffa_msg_send_direct_req", "ffa_msg_send_direct_req2")
MAX_TRACE_BYTES = 8 * 1024 * 1024
OPTIONS = {"funcgraph-abstime": "1", "funcgraph-proc": "1",
           "funcgraph-duration": "1", "funcgraph-tail": "1",
           "funcgraph-retval": "1", "funcgraph-retval-hex": "1",
           "funcgraph-args": "1", "funcgraph-retaddr": "0"}


def read(path):
    return path.read_text(encoding="ascii").strip()


def write(path, value):
    path.write_text(value + "\n", encoding="ascii")


def global_controls(root):
    names = ("current_tracer", "tracing_on", "trace_clock", "set_ftrace_filter",
             "set_graph_function", *("options/" + name for name in OPTIONS))
    return {name: read(root / name) for name in names if (root / name).exists()}


def preflight(root=TRACE_ROOT):
    available = {line.split()[0] for line in read(root / "available_filter_functions").splitlines()
                 if line.strip()}
    functions = [name for name in FUNCTIONS if name in available]
    if not functions:
        raise RuntimeError("no reviewed FF-A tracing functions are available")
    if "function_graph" not in read(root / "available_tracers").split():
        raise RuntimeError("function_graph tracing is unavailable")
    if not (root / "instances").is_dir():
        raise RuntimeError("private tracing instances are unavailable")
    return {"functions": functions, "global_tracer": read(root / "current_tracer"),
            "global_tracing_on": read(root / "tracing_on"), "global_controls": global_controls(root)}


def bounded_duration(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise argparse.ArgumentTypeError("duration must be between 1 and 30 seconds")
    return seconds


def capture(output, seconds, *, root=TRACE_ROOT, sleep=time.sleep, identity=None):
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise ValueError("duration must be between 1 and 30 seconds")
    info = preflight(root)
    # New, private output only; never overwrite a prior capture.
    output.mkdir(mode=0o700)
    instance = root / "instances" / f"dgx-ffa-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    report = {"schema_version": 1, "scope": "linux_ffa_calls",
              "identity": identity,
              "kernel": platform.release(), "started_at": datetime.now(timezone.utc).isoformat(),
              "requested_seconds": seconds, "instance": instance.name,
              "collector_firmware_requests": 0, "preflight": info,
              "enabled_options": [], "cleanup_complete": False, "status": "failed",
              "limitations": [
                  "Not an eSPI wire capture or secure-partition RAM/log dump.",
                  "Arguments are Linux function arguments; pointers are not dereferenced by this collector.",
                  "A Linux return value does not authenticate inner firmware or EC status.",
                  "Calls may involve any FF-A partition; the trace cannot always assign an endpoint.",
                  "Tracing changes timing; absent or unfinished calls are not proof of a firmware stall.",
              ]}
    created = False
    failure = None
    try:
        instance.mkdir()
        created = True
        write(instance / "tracing_on", "0")
        for name in ("current_tracer", "set_ftrace_filter", "trace_clock"):
            if not (instance / name).exists():
                raise RuntimeError(f"instance-local {name} is unavailable; no global fallback")
        write(instance / "buffer_size_kb", "64")  # Per CPU, fixed and bounded.
        clocks = read(instance / "trace_clock").replace("[", "").replace("]", "").split()
        if "mono" not in clocks:
            raise RuntimeError("instance monotonic trace clock is unavailable")
        write(instance / "trace_clock", "mono")
        names = "\n".join(info["functions"])
        write(instance / "set_ftrace_filter", names)
        selected = {line.split()[0] for line in read(instance / "set_ftrace_filter").splitlines()
                    if line.strip() and not line.startswith("#")}
        if selected != set(info["functions"]):
            raise RuntimeError("instance function filter did not match the fixed allowlist")
        # set_ftrace_filter also restricts function_graph. set_graph_function
        # may be global-only and expands descendants, so do not use it.
        # Filters are installed before selecting or starting the tracer.
        write(instance / "current_tracer", "function_graph")
        for name, value in OPTIONS.items():
            path = instance / "options" / name
            if path.exists():
                write(path, value)
                if value == "1":
                    report["enabled_options"].append(name)
        report["buffer_total_size_kb"] = read(instance / "buffer_total_size_kb")
        report["capture_begin_monotonic_ns"] = time.monotonic_ns()
        write(instance / "tracing_on", "1")
        sleep(seconds)
        report["status"] = "complete"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except (OSError, RuntimeError) as exc:
        failure = exc
        report["error"] = str(exc)
    finally:
        if created:
            cleanup_errors = []
            try:
                write(instance / "tracing_on", "0")
                report["capture_end_monotonic_ns"] = time.monotonic_ns()
                with (instance / "trace").open("rb") as source:
                    data = source.read(MAX_TRACE_BYTES + 1)
                report["trace_truncated"] = len(data) > MAX_TRACE_BYTES
                (output / "trace.txt").write_bytes(data[:MAX_TRACE_BYTES])
                report["cpu_stats"] = {p.parent.name: read(p)
                                       for p in sorted((instance / "per_cpu").glob("cpu*/stats"))}
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(str(exc))
            # Attempt all cleanup steps even when saving the trace failed.
            for name, value in (("tracing_on", "0"), ("current_tracer", "nop")):
                try:
                    write(instance / name, value)
                except OSError as exc:
                    cleanup_errors.append(str(exc))
            try:
                instance.rmdir()
            except OSError as exc:
                cleanup_errors.append(str(exc))
            report["cleanup_complete"] = not cleanup_errors
            if cleanup_errors:
                report["cleanup_errors"] = cleanup_errors
                report["status"] = "failed"
        try:
            report["global_controls_after"] = global_controls(root)
            report["global_controls_unchanged"] = report["global_controls_after"] == info["global_controls"]
        except OSError:
            report["global_controls_unchanged"] = None
        (output / "metadata.json").write_text(json.dumps(report, indent=2) + "\n", encoding="ascii")
    if failure:
        raise RuntimeError(f"capture failed; see {output / 'metadata.json'}: {failure}") from failure
    if not report["cleanup_complete"]:
        raise RuntimeError(f"cleanup incomplete; see {output / 'metadata.json'}")
    return report


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="inspect support without changing tracing")
    parser.add_argument("--seconds", type=bounded_duration, default=5.0)
    parser.add_argument("--output", type=Path, help="new directory for trace.txt and metadata.json")
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps(preflight(), indent=2))
            return 0
        if args.output is None:
            parser.error("--output is required for a capture")
        if os.geteuid() != 0:
            raise RuntimeError("capture requires root access to tracefs")
        if read(Path("/proc/sys/kernel/ftrace_enabled")) != "1":
            raise RuntimeError("global ftrace is disabled; it will not be enabled by this collector")
        signal.signal(signal.SIGTERM, interrupted)
        report = capture(args.output, args.seconds, identity={
            "hostname": platform.node(), "boot_id": read(Path("/proc/sys/kernel/random/boot_id"))})
        print(f"{report['status']}: {args.output / 'trace.txt'}")
        return 130 if report["status"] == "interrupted" else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

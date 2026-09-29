# Passive Linux FF-A call trace

This collector records **existing Linux FF-A calls** in a private ftrace
instance. It sends no firmware requests and can run with the existing Linux
clients bound. It does not load/unbind modules, consume EC data registers or
enable the global ftrace switch.

It provides part of a packet sniffer's utility: timestamps, process context,
call durations, and kernel-supported function arguments/return values.
It does **not** capture physical eSPI packets, secure-partition background
transactions, packet buffers behind pointers, or private firmware RAM.

## Capture

Run from the repository on the Spark:

```sh
sudo python3 diagnostics/ffa-trace/collect.py --check
mkdir -p local
sudo python3 diagnostics/ffa-trace/collect.py --seconds 5 --output local/ffa-capture
sudo cat local/ffa-capture/trace.txt
sudo cat local/ffa-capture/metadata.json
```

`--check` reads tracing capabilities without creating an instance. Capture
additionally requires instance-local controls; it refuses instead of using
global controls if those are unavailable. The output directory must be new
and is created with mode `0700`. Keep raw captures outside Git.

Duration is bounded to 1–30 seconds. The buffer is fixed at 64 KiB per CPU;
the exported trace is capped at 8 MiB and truncation is recorded. Only these
available functions are selected, with the filter read back before tracing:

- `ffa_sync_send_receive`
- `ffa_sync_send_receive2`
- `ffa_msg_send_direct_req`
- `ffa_msg_send_direct_req2`

The collector uses the instance's `set_ftrace_filter`, which also restricts
the function-graph tracer. It does not use the potentially global
`set_graph_function` selector. Optional function-graph controls request
timestamps, process context, durations, arguments and raw hexadecimal return
values; unsupported options remain absent and are listed in metadata.
These controls follow the [Linux ftrace documentation](https://www.kernel.org/doc/html/latest/trace/ftrace.html).

Completion, Ctrl+C and SIGTERM stop tracing, save available records and CPU
statistics, and remove the private instance. Configuration errors also clean
up. Exit 0 means capture completed, 130 means interrupted with cleanup, and
1 means failure; inspect `metadata.json` for details. An uncatchable kill or
machine failure cannot guarantee cleanup. The metadata names the private
instance if manual cleanup is needed.

## Interpret the result

Check `cleanup_complete`, `global_controls_unchanged`, `trace_truncated` and
the per-CPU overrun/drop statistics before interpreting timing or absence.
`identity.boot_id` identifies the boot, and monotonic timestamps can be
correlated with `dgx-power-control --debug` output.

Argument formatting depends on kernel support and type information. A Linux
pointer is not a packet payload. Return values are raw registers; interpret
an `int` using its low 32 bits, and ignore values for void functions. A zero
Linux return does not prove an inner firmware operation succeeded: this
investigation already found firmware paths that discard an EC error status.

The selected calls can serve **any FF-A partition**, not only partition 8003.
Timing and function names alone may not identify the endpoint. An empty
capture means no selected calls were recorded during that interval; it does
not imply that secure firmware or the EC was idle. An unfinished call at a
capture boundary does not establish a stall. Tracing itself adds timing
overhead, so this is passive with respect to firmware requests, not free of
observer effects.

For the separate private log ring and why its proposed export path has not
been established, see [firmware log visibility](../../docs/firmware-log-visibility.md).

# Validation and provenance

## Version 0.1.0 build and contract checks

On **2026-09-26**, all **19 project tests** passed on a DGX Spark (Python
3.12.3, GCC 13.3). They include **24 actual-C scenarios**. These run the
driver's register tables, `_DSM` map validation, hwmon callbacks, limit writes,
and restoration against a simulated SPBM page. The cases cover map
mismatch, omission, duplication, and malformed packages; unit conversion and
implausible values; NVIDIA-bounded ranges; unpublished EC limits; firmware
that ignores, delays, or disagrees with a request; foreign and racing writers;
retry past a foreign slot; and the removal write fence. Tests assert the
number of writes and update requests, and that nothing outside the four OS
slots and `UPDATE_SPBM` is written.

Eleven deliberate mutations each removed one safety check: NVIDIA ceiling,
unpublished-EC refusal, ownership, foreign-slot restore, applied-value
verification, removal fence, duplicate detection, stale/retry precedence,
racing-writer undo, sub-milliwatt rejection. The tests detected every one.

The same Spark built the driver with `W=1` against **`7.0.0-1019-nvidia`** and
**`6.17.0-1032-nvidia`**. `scripts/build-sign` signed both builds with an existing
enrolled certificate. There were no driver warnings; Kbuild reported the usual
compiler command-name difference and unavailable `vmlinux` for BTF. The
7.0 build has `srcversion` **`A7DD657F8421910B9541523`** and no modalias, so it
never loads automatically.

On that Spark, DMI reports `NVIDIA` / `NVIDIA_DGX_Spark` / `P4242`. The live
DSDT (SHA-256 `1009b952…df808f9d`) is byte-identical on two reference Sparks.
Statically decompiled, its `NVDA8800` `_DSM` lists 207 SPBM register names. All
54 pinned names appear exactly once at their pinned offsets. `NVDA8800:00` is
enumerated as a platform device, and its second memory resource is the 4 KiB
page at `0x1c238000`.

An earlier read-only diagnostic module, not this driver, read 27 of these
registers live on the same Spark after validating their offsets through
`_DSM`. OS and UEFI limit slots were zero. Applied limits equaled EC limits of
140/142 W package and 231/244 W system. A second Spark with pinned low clocks
showed zero EC slots and applied limits of 20 W package and 30 W system.

## Read-only hardware validation, 2026-09-26

The development driver with `read_only=1` was built with `W=1` for
`7.0.0-1019-nvidia`, signed with each machine's existing enrolled key,
temporarily loaded, and unloaded on two P4242 Sparks with EC 3.5.8 and SoC
2.155.11. Both passed the ACPI resource/register-map checks and registered
hwmon. All four cap attributes were mode `0444`. Removal reported no owned
limits to restore. No power-limit write or update request was issued.

| Result | Affected unit | Healthy control |
| --- | --- | --- |
| Applied PL1/PL2 | 20/20 W | 140/142 W |
| Applied SysPL1/SysPL2 | 30/30 W | 231/244 W |
| Published ceiling reads | `ENODATA`, all four | 140/142/231/244 W |
| `diagnose --json` | `nvidia_limits_unpublished`, exit 2 | `limits_match_nvidia`, exit 0 |
| Boot ID before/after | Unchanged | Unchanged |

Power, averaged inputs, energy, temperature, and limit-floor attributes were
read through this driver on both machines. The only attribute errors were
the four expected unpublished ceilings on the affected unit. This validates
readability and the observed values, not independent sensor calibration.
The four observed floors were 100 mW, rather than the larger values used in
the simulated firmware model. No assumption about a recommended minimum
operating power follows from those raw floors.

The owner had stopped inference before these captures; the worker PID lists
were empty before and after. No service or host restart was performed by the
investigation. The diagnostic modules are unloaded and no startup installation
was made. Raw logs, source hashes and collection scripts remain in ignored
`local/`, not in the repository history.

All 22 Python tests passed on the Spark, including the C simulation of
read-only permission, write rejection and removal with zero writes. The
userland diagnosis tests cover unpublished, partially unpublished, matching,
lower and above-ceiling applied limits, and read failures. The unchanged
signing test uses GNU `stat`; on macOS the initial full baseline suite had two
subtest failures from BSD `stat` before reaching the mocked build. Relevant
userland/C tests passed locally, and the full suite passed on target Linux.

## Passive EC diagnostic validation, 2026-09-26

The repository's optional `diagnostics/ec-publication` probe was built with
`W=1`, signed with each unit's existing enrolled key, loaded once and unloaded
on the same affected and healthy P4242 units. There were no driver warnings;
the usual compiler command-name and missing-`vmlinux` BTF messages remained.
The collector verified the kernel, DMI, SoC/EC inventory, DSDT digest, available
memory and unoccupied OEM endpoint before loading. An initial inventory-parser
mistake refused collection before loading; its corrected handling of current
versus lowest-supported versions is covered by a regression test.

The affected unit's 24 observations spanned 7.365 seconds: source budgets were
all zero at both endpoints, time remained 07:31:32 UTC, and seven packet changes
were observed. The healthy unit's capture spanned 7.362 seconds: package sources
were 140/142 W, system sources were 231/244/257/265 W, time advanced eight seconds,
and seven packet changes were observed. The offline analyzer distinguished
these states. Both runs ended with the probe unloaded, OEM endpoint unbound,
unchanged boot IDs and no inference workers. No new kernel errors appeared
during either capture. The pre-existing fan-control state was left untouched.

These are fixed passive reads, not an active request/response experiment or
proof of inner EC read success. The diagnostic sends no EC command packet or
event acknowledgement and makes no budget or reset write. No host/service
restart or persistent installation was performed.

All **30 project tests** passed on target Linux. New tests exercise incomplete
captures, invalid canaries and dates, partial publication, time regression,
missing/reordered samples, cleanup evidence and the inventory guard. The
probe's actual C read function is compiled and exercised against a fake FF-A
boundary, checking every address/length combination in the nearby region,
guarded output copying and transport failures. These tests do not validate
proprietary firmware semantics or perform hardware I/O.

| Hardware-tested source | SHA-256 |
| --- | --- |
| `diagnostics/ec-publication/dgx_ec_publication.c` | `1a88898babb9e0fa4cfb1278900868b8b07c013206757d2102251a99434a60af` |
| `diagnostics/ec-publication/collect.sh` | `df4965cd5abdf14727d259f6179a475806f85b72c64ce7bf5477db298cfb9d8c` |
| `diagnostics/ec-publication/analyze.py` | `e6383baed5781d2675c49a3a8fd2169731f0453c4b90c89f4242f16418a81bd3` |

Raw captures, signed binaries and prior research prototypes remain in ignored
`local/`. Only the bounded diagnostic source and summarized findings are tracked.

## Diagnostic revision and transport incident, 2026-09-26

The revised optional module is version **0.2.0**. It keeps the original five
address/length pairs, brackets each sample with version checks, compares two
budget reads at each endpoint, and returns the actual probe failure code.
V2 records include boot identity; the offline analyzer checks comparison
ordering and reports sample latency. V1 captures remain readable with their
weaker checks explicitly identified. `inspect_clients.py` inventories FF-A
sysfs bindings without sending any firmware request.

An excluded experiment attempted one 81-byte read spanning budgets and the
version canary on the healthy reference unit at **13:20 CEST**. Its canary
failed. Concurrent fan-client requests began returning firmware status 5;
the daemon exhausted its retries, could not restore automatic control, and
exited at 13:20:07. A later attempt using the original five-byte version read
returned all zeros and the revised module refused it with `-EBADMSG`.
Both probes unloaded. This is treated as an investigation-induced transport
incident, not a successful diagnostic capture. Its precise cause remains
unresolved; the fan-control service remains failed and its floor unverified.
The 81-byte request was not repeated and was never sent to the affected unit.
The larger-read implementation is absent from Git; its evidence is private.

The collector now refuses any bound Linux client sharing partition `8003`,
including other UUIDs. On the reference unit, the actual collector refused
at a bound `nvidia-ffa-ec` endpoint before loading a module or printing capture
data. The separate inventory found nine bound clients on the affected unit.
This restricts live collection on the normal configuration; unbinding those
clients is not an approved isolation method. No further live EC requests were
made in that validation run after the failed small-read check. A later single
read through the installed fan driver's own guarded path still returned
submit status `0x05`; see the
[fan-recovery assessment](no-restart-recovery.md#existing-fan-control-recovery).

A separate temporarily loaded **read-only SPBM driver**, which makes no EC
transport requests, still observed 140/142 W package and 231/244 W system
limits on the reference unit at 13:26 CEST. At 13:27 CEST the affected unit
still had missing ceilings and the 20/20 W package, 30/30 W system fallback.
Both SPBM modules unloaded, both boot IDs stayed unchanged, and no host,
EC or service restart was performed. Those SPBM values do not establish
transport, fan-policy or GPU health.

All **34 project tests** passed on target Linux. The revised C module built
with `W=1` on both units; only the existing compiler-name and missing-BTF
messages appeared. Tests exercise early and trailing canary failure, differing
budget reads, unchanged outputs after failure, no retry, shared-partition
client refusal, v1/v2 framing and comparison chronology. Twelve diagnostic
tests also passed on macOS. **A complete successful v2 hardware capture has
not been obtained**, and the comparison command has only synthetic v2 evidence.

| Revised source built/tested | SHA-256 |
| --- | --- |
| `diagnostics/ec-publication/dgx_ec_publication.c` | `e3d765df60b18eea627f0a411cf76479924707dd80f3b9008c2b03edae29fb95` |
| `diagnostics/ec-publication/collect.sh` | `8614b1b03e597bca9cdae88bd41967771b307f7b52a9580ee32eee76cf634ca5` |
| `diagnostics/ec-publication/analyze.py` | `a2cc2108f04769c7bcc81789d2cf4e70927ea1e7567ca3fe6d5bb4ec5ff4d4ce` |

## Offline incident replay, 2026-09-26

The [dgx1 incident analysis](dgx1-transport-incident.md) adds an exact-source
timeline and a capsule-hash-pinned emulator for the original SoC read wrapper.
With Unicorn 2.1.4, all nine fixed read cases and the initialization-prefix
configuration check passed. A mismatched capsule was refused before execution.
The replay verifies chunking, partial output, early failure, and housekeeping
control flow; bus behavior and housekeeping internals are simulated. It does
not reproduce the persistent transport failure or prove its cause. The
configuration prefix requests a 4096-byte maximum read and 64-byte maximum
payload, rather than establishing a 64-byte maximum read.

`./scripts/check` also passed all 34 tests on macOS with Homebrew GNU coreutils
first in `PATH`, including the signing test's GNU `stat` dependency. No driver
source changed, no firmware was included in Git, and this investigation made
no further live EC requests. These are local results, not a GitHub CI result.

## Offline transport replay, 2026-09-26

The second hash-pinned replay executes the original lower reader, manual-FIFO
transaction, cleanup, housekeeping and alert paths against a synthetic
controller. All **11 transport scenarios and two general-configuration cases**
passed with Unicorn 2.1.4. A mismatched capsule was refused before execution.
The cases verify isolated errors followed by healthy reads, incomplete
completion validation, deferred GET_PC error handling, and a conditional
retained-completion failure followed by a modeled drain. Model assumptions,
falsification criteria and firmware addresses are recorded in the
[incident analysis](dgx1-transport-incident.md#lower-transport-replay).

This establishes behavior of original instructions under supplied inputs;
neither the real controller's filtering/queue policy nor hardware recovery
has been verified. The target model's pending completion is not a measurement
from dgx1. No new live EC request, driver change, firmware flash, service
restart, or reset was performed. A logs-only check on dgx1 added no lower-level
error cause. A depth-limited directory-name inventory on dgx3 found generic
Linux tracing but no identified eSPI firmware trace export; no debugfs data
handler was read or trace enabled. Both recorded boot IDs remained unchanged.

All **34 project tests** again passed locally on macOS with GNU coreutils first
in `PATH`. The transport replay is an optional capsule-dependent check, outside
the standard suite; firmware is not distributed with tests. These results do
not imply a GitHub CI run or a successful v2 hardware capture.

## Offline EC lifecycle replay, 2026-09-26

The hash-pinned EC 3.5.8 replay passed **14 scenarios and an eight-input
power-state decoder check** using Unicorn 2.1.4. It executes original Thumb
publication and initialization paths with simulated I2C, GPIO and RTOS
boundaries. Checks include metadata preservation, clearing of existing budgets
and packet data, initializer gating, asymmetric boot-ready signaling, system
publication after a timeout, and erasure of published system limits on a
supplied later state transition. RTC success/error behavior and the exact
power suspend/resume group are also checked. See the
[lifecycle analysis](ec-lifecycle-replay.md) for scope and interpretation.

The replay refused a mismatched capsule and an entry outside its reviewed
instruction ranges. All **34 project tests** passed locally with GNU coreutils
first in `PATH`. This optional capsule-dependent replay is separate from the
standard test suite. No driver changed, no new hardware validation was
performed, and no live EC request, service restart, firmware write or reset
was made during this follow-up. Neither dgx3 recovery nor dgx1 transport
recovery has been demonstrated. These are local checks, not a GitHub CI result.

## Offline EC producer replay, 2026-09-26

The separate hash-pinned producer replay passed **14 cases and a three-bus
topology check** with Unicorn 2.1.4. Original instructions publish package
limits despite returned sensor-I2C errors or a failed notification. Supplied
mutex waits distinguish the RTC and sensor buses; a notification wait occurs
after the package stores. Two supplied event orderings let queries continue
after boot-ready is cleared while RTC waits for it, despite a pending RTC
timer event. The timer posts that event before work submission. These cases
stop at explicit boundaries and do not execute the full thermal policy, I2C
controller, RTOS scheduler or physical hardware. Details and assumptions are
in the [diagnostic README](../diagnostics/ec-publication/README.md#offline-firmware-replay).

A mismatched capsule and unreviewed execution entry were refused. All **34
project tests** passed locally on macOS with GNU coreutils first in `PATH`.
No driver changed or hardware experiment ran; neither machine was restarted.
Reading investigation documents over SSH sent no EC requests. No live cause
or recovery is established, and these checks are not a GitHub CI result.

## Offline mailbox waits and retry boundaries, 2026-09-26

The hash-pinned [mailbox-wait replay](../diagnostics/ec-publication/replay_ec_mailbox_waits.py)
passed **10 cases** with Unicorn 2.1.4. It executes the original status helpers,
notification path and nested mutex calls, with ownership, scheduling, delays
and hardware status modeled explicitly. Finite output-buffer polling releases
the mutex on timeout; acquisition of a mutex held by another task remains
indefinite. Supplied data consumption lets a running owner finish but does not
release a paused owner's lock. A thermal-path case verifies package stores
precede the mailbox wait. A mismatched capsule and an unreviewed execution
entry were refused.

The transport replay again passed **11 cases and two configuration checks**.
Its conditional retained-completion case now tries five fixed read
address/length pairs with different alignments. All issue new requests and
fail before GET_PC; varying the read boundary does not consume the modeled
pending completion. Real target-credit behavior remains unverified.

All **34 project tests** passed locally on macOS with GNU coreutils first in
`PATH`. Saved captures and a logs-only dgx3 check were reviewed; the recorded
boot ID was unchanged and no live task/owner dump was found. No EC request,
consuming DATA read, driver change, restart or reset was performed in this
follow-up. The live stalled owner and a recovery method remain unidentified.
These optional capsule-dependent checks are separate from the standard suite
and do not constitute hardware validation or a GitHub CI result.

## Offline startup and ownership cross-check, 2026-09-26

The expanded producer replay passed **16 cases and the three-bus topology
check**. Two new cases preserve the thermal thread context across window
initialization and supply arrival at its next loop head. The initializer
clears `0x11aa99`, and both successful and returned-error sensor transactions
are attempted again. All ten sensor writes repeat; package limits and fan
floors are published. This does not execute intervening thermal policy or
the scheduler.

The new [timer replay](../diagnostics/ec-publication/replay_ec_timer.py)
passed **three cases** using Unicorn 2.1.4. Original startup, timer-start and
expiry instructions initialize the periodic timer before static-thread setup
and queue the next expiry before posting the RTC event and submitting
separate work. A supplied work-submission error does not undo the preceding
queue insertion or event post. Other startup hooks, the timeout queue, tick
source and event/work boundaries are simulated. See the
[startup cross-check](ec-startup-cross-check.md) for the static evidence and
the corrected command-to-lock map.

Both replays refused a mismatched capsule and an unreviewed execution entry.
All **34 project tests** passed locally on macOS with GNU coreutils first in
`PATH`. The updated external research documents were read over SSH into
ignored review snapshots; their working files were not edited. No live EC
request, driver change, service restart, reset or new hardware validation
occurred. The live stalled task and no-restart recovery remain unestablished.
These are local results, not a GitHub CI result.

## Passive Linux FF-A tracing and command debug logs, 2026-09-26

All **41 project tests** passed locally on macOS with GNU coreutils first in
`PATH`. New checks compare the exact attribute-read sequence with debug
disabled/enabled, preserve clean JSON stdout and ENODATA/EIO behavior, and
exercise passive-trace filtering, bounded duration/export, refusal and
interruption/failure cleanup using temporary files only.

On dgx3, kernel `7.0.0-1019-nvidia`, a first collector attempt refused an
unavailable instance-local `set_graph_function` control before enabling the
tracer and removed its instance. The final collector uses only the verified
instance-local `set_ftrace_filter`; it never falls back to a global selector.

A **five-second passive capture** then completed with function-graph
argument/return options enabled, a monotonic clock and 64 KiB requested per
CPU. The kernel reported 1340 KiB total over 20 CPUs. It recorded **zero
selected Linux FF-A calls**, with no reported CPU overruns/drops and no
export truncation. This validates setup, timed capture and cleanup, **not**
argument decoding or recording a real transaction. It establishes neither
firmware inactivity nor communication health.

The private instance was removed; the pre-existing `rasdaemon` instance
remained. Checked global tracer, enable, clock, filters and graph-option
values were unchanged. Host boot ID remained
`f76ca9d3-4100-400c-b4bb-66d24e73c59d`. The collector submitted no firmware
request, loaded no module, and performed no reset or service restart.
Captures and metadata are retained under ignored `local/ffa-trace-dgx3/`.

The updated command was also run on dgx3 with `--debug diagnose --json`.
It logged hwmon discovery and exited 1 because the project's hwmon device
was not loaded; no module was loaded to change that condition. Successful
diagnosis output and error semantics are covered by the temporary-tree
tests. This live check is an error-path validation, not a fresh limit capture.

Tested source hashes:

| File | SHA-256 |
| --- | --- |
| `diagnostics/ffa-trace/collect.py` | `f1a04282f4cc379cc67ac38fbb2b8d26aad962189750e10137f2091430c04319` |
| `userspace/dgx_power_control.py` | `b8c803b77fe45e2d5a6054778cf229b02843ca31e5635d401d34315ca4a9a744` |

The [private-log review](firmware-log-visibility.md) verifies the ring writer
but identifies the proposed exporter as a configuration store. No firmware
log export endpoint, private-RAM dump, physical eSPI packet capture or live
recovery was obtained. These results do not imply a GitHub CI run.

## Not yet validated

- **No power-limit write has been performed.** Arbitration is inferred from
  EC-only observations and spark_hwmon's behavior. The one-second settle bound
  and the firmware's handling of the update request are untested.
- The write-enabled mode has not been loaded or used in this investigation.
- Absolute firmware-maximum registers are read internally for ceiling
  validation but have no independently validated public measurement.
- Suspend, reboot, and removal restoration are exercised only in simulation.

Any later power-control validation would, between jobs, lower `syspl1` briefly,
confirm the applied value and the effect under load, restore it, and check the
removal log. Results belong in this file.

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
made after the failed small-read check.

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

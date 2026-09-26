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

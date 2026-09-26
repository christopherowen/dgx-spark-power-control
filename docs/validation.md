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

## Not yet validated

- **This driver has not been loaded on any Spark.** Probe, hwmon
  registration, and the platform binding are verified only by compilation
  and static checks.
- **No power-limit write has been performed.** Arbitration is inferred from
  EC-only observations and spark_hwmon's behavior. The one-second settle bound
  and the firmware's handling of the update request are untested.
- Limit floor/maximum, averaged limit input, energy, zone temperature, and
  `vcore`/`prereg`/`dla` power registers have never been read live.
- Suspend, reboot, and removal restoration are exercised only in simulation.

The planned hardware sequence is: read-only load and telemetry comparison on
one Spark. Then, between jobs, lower `syspl1` briefly, confirm the applied value
and the effect under load, restore it, and check the removal log. Results
belong in this file.

# DGX Spark Power Control

**See where your Spark's watts go. Cap them for a job, then return to NVIDIA's limits.**

Read system, SoC, CPU-cluster, GPU, and input-rail power from Linux `hwmon`,
with cumulative energy and SoC temperatures. For a quieter, cooler, or
power-budgeted job, lower the package or system power limits below NVIDIA's.
A small kernel driver reads the firmware's own power-budget page; a Python
command controls it. NVIDIA's firmware keeps enforcing its limits throughout.

## Start here

| I want to… | Guide |
| --- | --- |
| Install with Secure Boot | [Installation, signing, and enrollment](docs/installation.md) |
| Reuse the key enrolled for dgx-spark-fan-control | [Existing signing key](docs/installation.md#2-use-an-enrolled-signing-key) |
| Rebuild automatically for kernel updates | [DKMS installation](docs/installation.md#3a-dkms-builds-for-kernel-updates) |
| Read power, energy, and temperatures | [Telemetry](docs/usage.md#read-the-telemetry) |
| Cap power for a job | [Lower a limit](docs/usage.md#lower-a-limit-for-a-job) |
| Restore NVIDIA's limits afterward | [NVIDIA limits](docs/usage.md#return-to-nvidias-limits) |
| Check a Spark with pinned low clocks | [Unpublished limits](docs/troubleshooting.md#nvidia-limits-unpublished) |
| Investigate recovery without restarting | [Firmware investigation](docs/no-restart-recovery.md) |
| Capture EC publication progress | [Optional passive diagnostic](diagnostics/ec-publication/README.md) |
| Upgrade or remove the software | [Updates and removal](docs/maintenance.md) |

## A typical session

After [installation](docs/installation.md):

```sh
dgx-power-control status
sudo dgx-power-control set-limit syspl1 150   # sustained system power: 150 W
```

Run your workload normally. When finished, remove the added limit:

```sh
sudo dgx-power-control automatic
dgx-power-control status                     # each cap equals nvidia=...
```

Status shows each limit's firmware-applied cap beside NVIDIA's own:

```text
system=26.10W package=17.30W gpu=5.00W cpu_p=0.50W cpu_e=0.01W hottest=31.2C prochot=1 control=available
pl1 average=18.00W cap=140.00W nvidia=140.00W floor=...
syspl1 average=26.00W cap=150.00W nvidia=231.00W floor=... restricted
```

## What you get

The driver registers one `hwmon` device, `dgx_spbm_power`:

| Kind | Channels |
| --- | --- |
| Power, 14 | `sys_total`, `soc_pkg`, `cpu_gpu`, `cpu_p`, `cpu_e`, `vcore`, `dc_input`, `gpu`, `prereg`, `dla`, and the averaged input of limits `pl1`, `pl2`, `syspl1`, `syspl2` |
| Energy, 4 | `pkg`, `cpu_e`, `cpu_p`, `gpu` (cumulative) |
| Temperature, 8 | `tj_max`, four CPU clusters, `gpu`, `soc`, `dla` |
| Status | `prochot`, `pl_level`, `tj_max_c` (raw firmware values) |

The four limit channels add `power_cap` (the firmware-applied limit),
`power_cap_max` (NVIDIA's published limit), and `power_cap_min` (the firmware
floor). **Limits can only be lowered**. A cap cannot exceed NVIDIA's limit, even
where the firmware's absolute maximum is higher. Writing zero restores NVIDIA's
value. So do driver removal, suspend, and reboot. See the
[attribute reference](docs/usage.md#attribute-reference).

There is no raw register access, PID or budget tuning, counter clearing, or way
to raise a limit. This cannot make the Spark draw more power than NVIDIA allows.

## Compatibility

Written for **NVIDIA DGX Spark, board P4242**, with SoC firmware 2.155.11, EC
firmware 3.5.8, and kernel **`7.0.0-1019-nvidia`**. The driver maps one ACPI-described
4 KiB page and refuses to bind unless every register it uses matches the
firmware's own `_DSM` map at its pinned offset. Other GB10 systems, OEM
variants, and future firmware are unverified.

You need Python 3.10+, matching NVIDIA kernel headers and build tools, and an
enrolled local signing certificate when Secure Boot is enabled. DKMS is optional.

This is an independent, experimental project, unaffiliated with NVIDIA.
The development driver has been temporarily loaded in explicit read-only mode
on healthy and affected Sparks. Power-limit writes are not yet hardware-validated;
see [validation](docs/validation.md). The firmware's limit arbitration is
inferred, so every write is verified against the applied value. A
disagreement undoes the write.

## Development

Run `./scripts/check` (Python 3.10+ and a C compiler) for hardware-free tests:
source and packaging contracts, userland failure paths, the signing wrapper,
and the driver's actual C functions against a simulated SPBM page. GitHub
Actions runs the same checks. Kernel compilation and real firmware behavior
need a compatible Spark.

For diagnosis, load with `read_only=1` to disable every limit write, then run
`dgx-power-control diagnose --json`. The command reports published and applied
limits; it does not reset the EC or repair missing firmware data. See
[diagnostic usage](docs/usage.md#diagnose-unpublished-limits).

- [Firmware interface](docs/interface.md)
- [Validation record](docs/validation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)

## Acknowledgements

Channel selection, labels, and units follow
[spark_hwmon](https://github.com/antheas/spark_hwmon) by Antheas Kapenekakis,
which first exposed this interface. This driver is a separate implementation
with a pinned contract, an NVIDIA-bounded write range, and lifecycle
restoration; see [how it differs](docs/interface.md#differences-from-spark_hwmon).
Do not load both drivers.

## License

[GPL-2.0-only](LICENSE), matching spark_hwmon's SPDX identifier.
Copyright 2026 Christopher Owen.

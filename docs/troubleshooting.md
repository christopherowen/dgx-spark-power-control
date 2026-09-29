# Troubleshooting

Start with read-only observations:

```sh
uname -r
modinfo dgx_spbm_power_control
lsmod | grep -E 'dgx_spbm|^spbm'
dgx-power-control status
sudo journalctl -k -b | grep -i spbm
```

## Module rejected or hwmon device absent

Check that the module was compiled for the running kernel (`modinfo` reports
`vermagic`), and that its signing certificate is enrolled. `Key was rejected
by service` usually means signing or enrollment. `Invalid module format` requires
rebuilding for the exact kernel. `No such device` from `modprobe` means the DMI
identity is not a P4242 DGX Spark.

The module can load while probe refuses the device. Read the kernel log:

| Log message | Meaning |
| --- | --- |
| `MTEL register-map _DSM unavailable` | Firmware lacks the expected ACPI method |
| `_DSM resource 1 is not SPBM` / `unexpected SPBM resource` | ACPI resource layout changed |
| `register map lacks …` / `register map mismatch: …` | Firmware moved or removed a register |
| `SPBM telemetry inactive` | System power reads zero or implausible |

These are deliberate refusals. Report the firmware versions
(`sudo fwupdmgr get-devices`) and the log; do not remove the checks.

`spark_hwmon`'s `spbm` driver binds the same ACPI device through the ACPI bus;
this driver binds its platform device. Load only one of them.

## Control disabled

`control=disabled` in status can mean the module was intentionally loaded with
`read_only=1`; no reload is needed for diagnosis. Otherwise, `existing … OS limit
… power-limit control disabled` in the log means an OS limit slot was nonzero at probe time.
Telemetry still works. Another tool, usually spark_hwmon, set that limit. Clear
it with the same tool (with spark_hwmon: write `0` to its `power*_cap`), unload
that tool, then reload this driver:

```sh
sudo modprobe -r dgx_spbm_power_control
sudo modprobe dgx_spbm_power_control
dgx-power-control status
```

## NVIDIA limits unpublished

`nvidia=unpublished`, `ENODATA` on a write, or `NVIDIA … limit unpublished;
firmware applies … mW` in the log means NVIDIA's EC-sourced limit slot is zero.
The firmware then applies a fallback limit. On one reference Spark this was
20 W package and 30 W system, well below normal. That Spark's GPU stayed
near 550 MHz under load while `nvidia-smi` reported no clock-event reasons.

This driver refuses nonzero writes for those limits: there is no NVIDIA value
to bound them, and raising limits is out of scope. Zero SoC-side copies do not
identify the cause by themselves. A subsequent firmware investigation also
found zero EC-visible source limits and a stale EC time mirror on the affected
reference unit, pointing upstream of a lost SoC transfer. A limitation in
firmware's read-status reporting prevents treating a successful FF-A call alone
as proof of a successful EC read.

Use `dgx-power-control diagnose --json` to preserve the budget state without
writing. Loading, unloading, or using `automatic` cannot republish EC data.
A no-restart repair has not been demonstrated. An EC software-reset path was
identified, but it still depends on host power-down and remains untested.
See the [firmware investigation](no-restart-recovery.md) for the evidence,
excluded candidates, and what a live repair must establish.

## ESTALE, ETIMEDOUT, or restore errors

`ESTALE` means the OS slot no longer holds this driver's value: another writer
changed it. The driver will not overwrite it. Stop the other writer, then clear
the slot with that writer.

`ETIMEDOUT` means the firmware's applied limit did not equal the expected
value within one second. The driver has already attempted to restore NVIDIA's
value; the log states whether that worked. Repeated timeouts suggest
the inferred arbitration does not match this firmware. Report
the `status` output and log, with firmware versions.

`failed to restore NVIDIA power limits` at removal, suspend, or reboot leaves a
limit **lower** than NVIDIA's, never higher. Check status after the next boot.

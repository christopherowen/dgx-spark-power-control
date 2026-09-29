# Contributing

Issues and pull requests are welcome. Include your exact hardware, kernel,
SoC and EC firmware versions, and `dgx-power-control status` output when
reporting compatibility or power behavior. Please use small, reviewable changes
and describe how you checked them.

Run `./scripts/check` before submitting. Userland tests use temporary hwmon
trees and mocks; they must never read or write the test host's real sysfs.
Limit tests compile the driver's actual C functions against a simulated SPBM
page and firmware; they exercise refusal, timeout, ownership, and restoration
paths without touching hardware. Driver changes also need a `W=1` build on the
target kernel and an explicitly documented hardware validation result or
testing limitation.

Keep `PACKAGE_VERSION` in `dkms.conf` aligned with the driver's `MODULE_VERSION`
when releasing a driver version, and update versioned installation examples.

Keep the interface narrow: validated telemetry, the four OS limit slots bounded
by NVIDIA's published limits, and restoration. Do not add raw register access,
writes to any other register, PID or budget tuning, counter clearing, or any way
to raise a limit above NVIDIA's. A new register needs its `_DSM` name and a
pinned offset, verified at probe like the others. Keep documentation
consistent with error paths as well as successful behavior. Do not claim
vendor-documented semantics for inferred behavior.

Contributions are under GPL-2.0-only. Do not include proprietary firmware,
ACPI table dumps, signing keys, generated modules, private machine logs, or
credentials.
Keep hardware captures and logs under the ignored `local/` directory.

The optional `diagnostics/ec-publication` module has a separate, release-pinned
read-only contract because the EC SRAM fields have no `_DSM` map. It is not
built or installed with the hwmon driver. Keep its fixed allowlist and bounded
capture; do not add arbitrary memory access, packet submission, event
acknowledgement, or recovery writes to a diagnostic interface. Changes require
firmware-path review, a target-kernel build, and a recorded hardware result or
explicit testing limitation.

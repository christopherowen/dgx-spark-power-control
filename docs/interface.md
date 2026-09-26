# Firmware interface

These are reverse-engineering findings, checked against the shipped
implementation and live ACPI tables. They are not a vendor-published ABI
guarantee. No firmware image or extracted firmware code is distributed here.

## Path from userland to the limits

```text
dgx-power-control (Python, root for writes)
  -> /sys/class/hwmon/hwmonN/powerN_cap
  -> dgx_spbm_power_control (kernel)
  -> SPBM page: one OS limit slot, then UPDATE_SPBM
  -> SoC power-budget firmware
  -> applied limit = lowest published source (inferred)
  -> PID controllers and clock budgets (NVIDIA's, unchanged)
```

Telemetry takes the same path in reverse: 32-bit reads of firmware-maintained
registers, converted to hwmon units. No mailbox, FF-A, or EC transaction is
involved, and the driver issues no firmware requests while reading.

## Pinned contract

| Field | Accepted value |
| --- | --- |
| DMI vendor / product / board | `NVIDIA` / `NVIDIA_DGX_Spark` / `P4242` |
| ACPI device | `NVDA8800` (`\_SB.MTEL`), bound as a platform device |
| `_DSM` GUID, revision | `12345678-1234-1234-1234-123456789abc`, 0 |
| `_DSM` functions | 1 (resource names) and 2 (register maps), both advertised by function 0 |
| Resource 1 | Named `SPBM`; memory `0x1c238000`, 4 KiB |
| Register map | Function 2, argument 1: all 54 pinned names, each exactly once at its pinned offset |

Function 2 returns packages of `{count, name, offset, ...}`. The driver
requires that shape, ignores names it does not use, and refuses to bind on a
missing, duplicated, or moved register. It then maps only that page and
requires nonzero, plausible system power. A firmware update that moves any
used register makes the driver refuse to load rather than read the wrong words.

Units are firmware milliwatts, millijoules, and centidegrees Celsius, converted
to hwmon microwatts, microjoules, and millidegrees. Power and limit values above
1,000 W, and temperatures above 200°C, return `EBADMSG`.

## Registers

| Use | Offsets |
| --- | --- |
| Update request, write-only use | `0x000` |
| Status: `pl_level`, `prochot` / `tj_max_c` | `0x048`, `0x04c` / `0x81c` |
| Telemetry power | `0x300`–`0x314`, `0x31c`, `0x324`, `0x334`, `0x338` |
| Cumulative energy | `0x344`, `0x350`, `0x35c`, `0x374` |
| Averaged limit input (PL1, PL2, SysPL1, SysPL2) | `0x800`, `0x804`, `0x80c`, `0x810` |
| Temperatures | `0x818`, `0x820`–`0x838` |

Each of the four limits has six registers:

| Source | PL1 | PL2 | SysPL1 | SysPL2 |
| --- | --- | --- | --- | --- |
| OS request (written) | `0x100` | `0x104` | `0x110` | `0x114` |
| EC (NVIDIA) | `0x120` | `0x124` | `0x130` | `0x134` |
| UEFI | `0x140` | `0x144` | `0x150` | `0x154` |
| Applied | `0x160` | `0x164` | `0x170` | `0x174` |
| Firmware floor / maximum | `0x708` / `0x70c` | `0x718` / `0x71c` | `0x738` / `0x73c` | `0x748` / `0x74c` |

Names are in the source table. The firmware map also lists PL3/PL4 slots, PID
gains and outputs, budgets, and overflow/clear registers. This driver leaves
all of those unread and unwritten.

## Limit arbitration

On healthy reference Sparks, OS and UEFI slots were zero and each applied
limit equaled its EC value (PL1/PL2 140/142 W, SysPL1/SysPL2 231/244 W). From this,
and from spark_hwmon's reported behavior, the driver **assumes**:

```text
applied = lowest nonzero of (OS, EC, UEFI)
```

With every source zero, the firmware applies an unmodeled fallback. One Spark
with pinned low GPU clocks showed all EC slots zero and applied limits of only
20 W (package) and 30 W (system). EC slots are the SoC's copies of the EC's
limits. Zeros show that no copy was published, not why.

## Writes and ownership

A write stores one value in one OS slot, then writes 1 to `UPDATE_SPBM`. The
firmware's own EC-limit transfer uses the same update request. `iowrite32`
orders the two stores. Those five registers are the only ones the driver ever
writes. ACPI `_CRS` describes the page as read-only memory, but its map names the
OS slots as OS-sourced limits. That declaration and write acceptance by
firmware are both recorded here as observed facts, not a vendor guarantee.

Accepted values lie from the firmware floor to the lowest published NVIDIA
limit: `min(EC, UEFI if nonzero, firmware maximum)`. A zero EC slot refuses
writes with `ENODATA`. After writing, the driver polls the applied register
every 20 ms for up to one second. It requires the value predicted by the
arbitration above. On timeout, it restores that OS slot to zero and verifies
NVIDIA's value, unless another writer has changed the slot. If the assumption is
wrong, the result is a refused write, never an unverified success.

Probe requires all four OS slots to be zero; otherwise it binds with limit
control disabled. The driver remembers each value it wrote and which limits
still need applied-value verification. A slot that no longer holds the driver's
value returns `ESTALE` and is never overwritten. A second writer using the same
value cannot be distinguished, so only one driver should control these slots.

## Lifecycle

Driver removal and orderly reboot first fence further writes, then restore
every limit the driver owns. Suspend restores without fencing; limits are not
reapplied on resume. Restoration makes up to three attempts, 100 ms apart. A
foreign slot is final. Logs distinguish restored limits, a no-op, and failure.
If no limit is published when restoring, zeroing the OS slot cannot be
verified; the log says so. A hard crash or power loss skips restoration.
Whether firmware clears OS slots across a warm reboot is unverified.

## Differences from spark_hwmon

| Behavior | spark_hwmon `spbm` 0.3.0 | This driver |
| --- | --- | --- |
| Binding | ACPI driver, auto-loaded by modalias | Platform driver, explicit load, DMI-gated |
| Register offsets | Discovered by name; unresolved channels hidden | Pinned; any mismatch refuses to bind |
| Cap ceiling | Firmware maximum (for example 250 W PL1) | NVIDIA's lowest published limit |
| Cap bounds attributes | `power_max` / `power_min` | hwmon ABI `power_cap_max` / `power_cap_min` |
| Write verification | None | Applied value polled; undone on mismatch |
| Existing OS limit at load | Retained; later writes replace it | Control disabled; slot left untouched |
| Removal, suspend, reboot | OS limits persist | Driver-owned limits restored |
| Implausible values | Reported | `EBADMSG` |

Telemetry labels and units match, so readings are directly comparable.

## Related primary documentation

- [Linux hwmon sysfs ABI](https://www.kernel.org/doc/html/latest/hwmon/sysfs-interface.html)
- [ACPI `_DSM`](https://uefi.org/specs/ACPI/6.5/09_ACPI_Defined_Devices_and_Device_Specific_Objects.html#dsm-device-specific-method)
- [spark_hwmon](https://github.com/antheas/spark_hwmon)

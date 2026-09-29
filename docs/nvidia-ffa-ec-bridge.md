# The NVIDIA FF-A EC bridge and USB-C PD

Status: corrected against the supplied dgx1 kernel/ACPI artifacts, dgx3
enumeration and the pinned EC/SoC images on 2026-09-26. The review issued
no live EC/PD request and changed no firmware state.

`nvidia-ffa-ec` is a generic ACPI-to-FF-A bridge, not a PD policy engine.
It nevertheless recognizes and binds a **UCSI service**. The secure
partition also contains UCSI handlers. A bound service does not imply a
working Linux Type-C connector interface: none was observed on these hosts.

## 1. Driver and service identities

The driver is built into Ubuntu kernel `7.0.0-1019-nvidia`; its embedded
source path names `drivers/platform/arm64/nvidia-ffa-ec.c` in the
`linux-nvidia-7.0` source package. The
[public NVIDIA source reference](https://github.com/NVIDIA/NV-Kernels/blob/f8c1047/drivers/platform/arm64/nvidia-ffa-ec.c)
provides service names and protocol structure. It is not claimed to be
the exact source revision of the running kernel. The UCSI rescan mapping
was independently verified in the supplied binary.

| Device | UUID prefix | Service name in the source reference | Rescan HID |
| --- | --- | --- | --- |
| `arm-ffa-7` | `330c1273` | Management | None |
| `arm-ffa-8` | `7157addf` | Power | None |
| `arm-ffa-9` | `25cb5207` | Battery | `PNP0C0A` |
| `arm-ffa-10` | `31f56da7` | Thermal | None |
| `arm-ffa-11` | `7697530c` | Fan | `PNP0C0B` |
| `arm-ffa-12` | `65467f50` | UCSI | **`PNP0CA0`** |
| `arm-ffa-13` | `e3168a99` | Input | None |
| `arm-ffa-16` | `23ea63ed` | Time/alarm | `ACPI000E` |

All eight are bound to `nvidia-ffa-ec`. “No rescan HID” does not mean
“notifications only.” The notification service has its own binding at
`arm-ffa-6`. The OEM read and fan endpoints used elsewhere in the
investigation are separate from this eight-entry bridge table.

The probe allocates a per-endpoint context, sets up notifications, adds it
to the service list under a mutex, and rescans ACPI for four UUIDs.
Battery, fan and UCSI ACPI devices were absent. **The time/alarm device is
present**: dgx3 has `ACPI000E:00`, path `\_SB_.RTC_`, bound to `acpi-tad`.
`ACPI000E` is a Time and Alarm Device; Generic Event Device uses `ACPI0013`.

### Binary verification of the UCSI HID

For the captured dgx1 boot, `_text` is `0xffffc14e23dd0000` and
`nvidia_ffa_ec_service_probe` is `0xffffc14e254126b0`. The file named
`/tmp/fwcaps/vmlinux` is a raw arm64 Image, not an ELF file. Probe offset
`+0x280/+0x284` materializes the UCSI rescan string address; the common
call to `acpi_bus_for_each_dev` is at `+0x17c`.

| String | Image offset |
| --- | --- |
| `PNP0C0A` | `0x23e1518` |
| `ACPI000E` | `0x24d3140` |
| `PNP0C0B` | `0x23ebd28` |
| `PNP0CA0` | `0x24d3150` |

The error message about an unavailable FF-A/notify device starts eight
bytes after the UCSI HID. The earlier placeholder claim confused those
adjacent strings. The first `0x2c0` bytes of the probe dump agree with
Image offset `0x16426b0`, except for its first instruction (`mov x9,x30`
in the dump versus `nop` in the Image).

## 2. AML-facing protocol and RTC

The reviewed DSDT declares `FFA0`, HID `MSFT000C`, with
`OperationRegion (AFFH, FFixedHW, 0x04, 0x90)`. `FFAC` is a
**1152-bit buffer field**, not a method. `AVAL()` takes no arguments and
returns one. AML accesses the field with the store/read expression
`BUFF = ^^FFA0.FFAC = BUFF`.

The Fixed Hardware region space is `0x7f`; searching only for `0x0b`
does not test for FFH. The packet envelope is:

| Byte offset | Field |
| --- | --- |
| 0 | Bridge status |
| 1 | Service-data length |
| 2–17 | UUID in AML byte order |
| `0x12` onward | Service-specific request/reply bytes |

`nvidia_ffh_handler` converts the UUID, finds the bound FF-A endpoint,
uses its direct-message interface, and copies the response back. Bridge
status is distinct from any status inside the service reply.

The RTC getter is `_GRT`; `BGRT` is its local result buffer. `_GRT`,
`_SRT`, `_GWS`, `_CWS`, `_STV`, `_TIV`, `_STP` and `_TIP` use the
time/alarm UUID `23ea63ed-b593-46ea-b027-8924df88e92f`. This establishes
the AML route, not an additional producer-recovery operation.

The DSDT's `arm-arml0002-ffa-ntf-bind` property belongs to **FFA0** and
names management UUID `330c1273-fde5-4757-9819-5b6539037502`, with IDs
1, 2 and 8. Its `_DSM` handles ID 8 by notifying the power button. No
corresponding RTC `_DSD` binding appears in this DSDT. Do not describe
this as an RTC GED notification map.

## 3. Secure-partition UCSI support and its limits

The supplied `soc_sp.bin` contains these strings:

| File offset | String fragment |
| --- | --- |
| `0x3f357` | `Get PD%d UCSI SHM fail` |
| `0x3f508` | `UCSI control cmd:` |
| `0x3f527` | `Send UCSI command:` |

Its first `0x41100` bytes exactly match the pinned service extraction.
There is corresponding code: dispatcher `0x93977d90`, block lookup
`0x93977658`, request staging `0x93977a14`, reply copying `0x939778a4`
and version copying `0x93977c5c`. The manifest declares nonsecure PD0/PD1
blocks at `0x933de000` and `0x933df000`.

The SoC selects EC mailbox methods 4/8 for PD0/PD1. In the pinned EC,
opcode 4 posts a zero event mask, and the inspected worker command branch
also polls a zero requested mask and therefore skips processing. Opcode
8 follows the default return. This constrains that route; it does not
exclude all PD access or all event-driven work in the controller task.

Live passive shared-memory captures on dgx3 and healthy dgx2 contained
no recognized UCSI header. Neither a current input contract nor a working
Linux UCSI connector interface has been established. Linux's lack of
Type-C class devices does not erase the firmware's UCSI implementation.

The SP also contains power-budget and other services, including a message
saying PSR is unimplemented. One unimplemented getter does not establish
that every host diagnostic path is absent.

## 4. Adapter and EC-side evidence

[ChargerLAB's adapter teardown](https://www.chargerlab.com/teardown-of-the-nvidia-dgx-spark-original-240w-usb-c-power-adapter)
identifies a Delta ADP-240LB B with a captive USB-C cable and WT6678F
source controller. It reports fixed offers through 48 V / 5 A and AVS
capability from 15–48 V. This is source capability evidence, not a capture
of dgx3 negotiating AVS. Fixed 48 V remains a possible operating contract.
The barrel-connector hypothesis is withdrawn. NVIDIA's
[hardware guide](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)
places the USB-C connectors on the rear panel.

The [MEC172x register map](https://www.microchip.com/content/dam/mchp/documents/CPG/ProductDocuments/DataSheets/MEC172x-Data-Sheet-DS00003583D.pdf)
corrects two earlier peripheral identifications:

| Address | Peripheral |
| --- | --- |
| `0x40007400` | RTOS timer |
| `0x40007c00` | ADC |
| `0x4000e000` | EC interrupt aggregator |
| `0x4000ae00` | VBAT-powered Control Interface |

These constants do not prove the origin of SPBM `dc_input`/`prereg`
telemetry, five initialized application buses, or an EC-resident PD policy
engine. An address histogram cannot exclude INA-family devices either.
The application I2C wrappers examined here accept logical buses 0–2.

There is stronger, transaction-level evidence for **Infineon/Cypress HPI**:
the table at `0x1184e4`, stride 12, selects targets `0x40` and `0x42`,
with two ports each. After initialization the HPI wrapper uses logical
bus 1 (`0x40004400`) and two-byte register addresses. Function `0xc6ba4`
reads PD status at `0x1008/0x2008`, tests contract bit 10, reads current
PDO at `0x1010/0x2010`, then Type-C status at `0x100c/0x200c`.
This matches the [official HPI register definitions](https://github.com/Infineon/hpi/blob/d6da831a1fd28caf2f2f44c60cc3c8cdd2c7634f/cy_hpi_defines_default.h).
It does not yet identify an exact controller part or physical connector.

The EC also contains helper `0xccd10`: conditional on an eligibility flag and
controller firmware version, it writes command `0x47` or `0x48` to
target `0x40`, register `0x1006`, then polls for a fixed PDO describing
48 V / 5 A or 20 V respectively. It has no proven independent host entry
and no demonstrated power-preserving behavior. Its command names and the
controller's handling of them remain unverified. GPIO VBUS enables are
therefore not the only compiled EC-side port-control path, although no
new host control has been established.

The flag at `0x11ab4a` is initialized by `0xc5db8`: a ROM call at
`0xc5dd0` invokes Thumb entry `0xf008` with selector `0x3f9`; return
status zero sets the flag, regardless of the output byte. The selector's
meaning is unknown because ROM is outside this application image. Four
direct voltage-helper calls were found: `0xc1d82`, `0xc2164` and `0xc5a16`
pass zero; only `0xc21ea` passes one. That high branch is followed immediately
by hardware power-up at `0xc1dd0`. Bounded offline queue-prefix execution
found no isolated high request in the supplied normal-state cases. It does
not exclude all later transitions or computed indirect calls.

Initializer `0xc784c` copies firmware-version byte pairs to EC window offsets
`0x170`–`0x177` and device-mode bits 1:0 to `0x19a/0x19b`, through writer
`0xc492c` at base `0x119000`. In the reviewed host map these are
`0x06000770`–`0x06000777` and `0x0600079a/0x0600079b`. All ten bytes
are preserved across window reinitialization by the table at `0x11892c`;
the getter ignores I2C status. These are retained identity samples, not
fresh contract data. This path does not copy the four private numeric
contract records at `0x11a160` to the host window. No live read was issued.

## 5. Recovery-focused next steps

1. Find an independent host-to-HPI entry. The audited voltage-helper
   callers are power/configuration sequences and provide no isolated
   running-system operation; knowing the gate alone does not change that.
2. Identify the HPI controller and power-input port, and find a host route
   to raw PD status/current PDO/current RDO with completion and freshness
   evidence. Cached values are insufficient.
3. Determine the controller-specific meaning and power effects of the
   voltage commands before considering an experiment.
4. Keep contract recovery distinct from recovery of the stalled RTC and
   budget producers; validate both if an entry is established.

The kernel fingerprint supports v3.7-compatible routines, not an exact
vendor source tree. PD-specific source matching should follow the HPI
evidence; proving that the entire Zephyr USB-C stack is linked is not a
prerequisite to tracing these existing routines.

## Provenance and correction record

The original dgx1 artifacts remain under `/tmp/fwcaps`. No capsules,
raw memory dumps or generated disassembly belong in Git.

| Artifact | SHA-256 |
| --- | --- |
| Kernel Image named `vmlinux` | `6220c8a5fca3340de6e0b3b7da5c834d1ff2868c90f2eeb171695ec393df45c6` |
| `ffa_ec_probe.bin` | `55fba98f07f818af4f9805497cd0201b612dd256c3f691ab25cabfd7d10c2725` |
| DSDT | `1009b95258a5fa8c85fe458b3019cd5658f099c3ba703cec3ec945b5df808f9d` |
| SoC service, first `0x41100` bytes of supplied `soc_sp.bin` | `13d8c21696053db4ca2fd64cba5ecef25fbe8a7ca14ce1d58001b3405e2741a8` |
| EC 3.5.8 capsule | `ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82` |

EC capsule offset `0xd9f` maps to VA `0xc0000`. SoC capsule offset
`0xa08f0f` maps to VA `0x93942000`. The old claims of an inert UCSI
HID, absent RTC, absent UCSI firmware, method-form FFAC, RTC GED binding,
ADC at the timer address and VCI at the interrupt aggregator are withdrawn.

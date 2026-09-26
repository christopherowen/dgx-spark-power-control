# Passive EC publication capture

This optional diagnostic belongs to the power-control repository. It adds
evidence below SPBM when `dgx-power-control diagnose` reports missing limits.
It does **not** implement a recovery command, replace the hwmon driver, or
install a daemon. It is excluded from the normal build, DKMS, and autoload.

**Live collection is currently restricted.** A larger-read experiment caused
a communication incident on the reference unit, described in the
[validation record](../../docs/validation.md#diagnostic-revision-and-transport-incident-2026-09-26).
The collector now refuses bound clients on the shared firmware partition,
including NVIDIA's built-in EC clients on a normally configured Spark. Do not
unbind those clients to bypass the refusal. Offline analysis and the regular
SPBM `diagnose` command remain usable.

Inventory the shared clients without root or any firmware request:

```sh
python3 diagnostics/ec-publication/inspect_clients.py
```

This reports every Linux endpoint sharing partition `8003`, its UUID and
bound driver, including clients missed by an OEM-endpoint-only check. Exit 0
means the inventory was read, not that collection is permitted.

The investigation currently targets P4242, kernel `7.0.0-1019-nvidia`, EC 3.5.8,
and SoC 2.155.11 only. `collect.sh` requires that exact reported inventory and
the validated DSDT digest. The module separately checks DMI, the FF-A endpoint,
API 1.2, and EC-version canaries. These are compatibility checks, not live flash
attestation. Do not bypass the collector to use another firmware version.

## Offline firmware replay

For investigation without hardware access, the optional
`replay_firmware_reads.py` executes the original read wrapper in emulated
memory. In a separate Python environment with `unicorn==2.1.4` installed, run:

```sh
python3 diagnostics/ec-publication/replay_firmware_reads.py /path/to/socfw.cap
```

It verifies the release capsule hash and checks nine fixed read/failure cases
plus the initialization prefix's channel configuration. Bus responses and
housekeeping are simulated. It neither loads a module nor contacts a machine,
and does not reproduce the physical transport incident. See the
[incident analysis](../../docs/dgx1-transport-incident.md) for interpretation
and the next evidence required. The capsule is not included in this repository.

The deeper transport replay executes the lower reader, FIFO transaction,
error cleanup, housekeeping and alert-handler instructions:

```sh
python3 diagnostics/ec-publication/replay_firmware_transport.py /path/to/socfw.cap
```

Its 11 scenarios check normal/failing reads, completion validation, deferred
responses and a **hypothetical** retained-completion failure followed by a
modeled drain. Two configuration cases check response-modifier handling.
Controller registers, FIFO data and target queue behavior are synthetic and
listed in the JSON output. The model does not establish dgx1's cause or provide
a live recovery command. Both tools reject a different capsule before executing
instructions and stop on unmodeled accesses or exhausted instruction budgets.

The EC lifecycle replay uses the separate EC 3.5.8 capsule:

```sh
python3 diagnostics/ec-publication/replay_ec_lifecycle.py /path/to/ec_fused.cap
```

It checks 14 fixed scenarios plus eight logical power-state inputs. Original
EC instructions exercise window initialization, publication, RTC error handling
and thread-group selection; I2C, GPIO and RTOS boundaries are simulated. It
demonstrates an ordering that clears previously published system limits without
republishing them, and checks explanations that do not fit all observations.
See the [lifecycle analysis](../../docs/ec-lifecycle-replay.md) for the assumptions
and remaining evidence. This hash-pinned, bounded replay never contacts an EC
and exposes no live recovery interface.

The producer replay extends this with original thermal startup, I2C wrappers,
RTC reads, query loops and timer callback instructions:

```sh
python3 diagnostics/ec-publication/replay_ec_producers.py /path/to/ec_fused.cap
```

Its 14 fixed cases distinguish a returned error from a wait that never returns.
All ten sensor-initialization writes can return errors and the thermal path
still publishes 140/142 W package limits. A failed notification also leaves
those stores intact. A supplied wait inside the notification stops before fan
initialization but after the package stores. These results constrain where an
uninterrupted first thermal iteration could stop; a later window clear can
still erase earlier publication.

The initialized device table and executed wrappers select separate controllers
and mutexes for RTC (bus 0, mutex `0x11869c`) and sensors (bus 2, mutex
`0x1186d4`). Holding one modeled mutex does not stop the other producer.
An RTC error that returns releases its mutex and publishes `FF` bytes; a
supplied mutex wait preserves the old mirror. Actual mutex owners and hardware
controller progress are outside the model.

Two explicit event orderings let a query pass boot-ready, execute PLTRST work
that clears boot-ready, then execute the timer callback and start RTC. RTC
waits for boot-ready even though its separate periodic-event bit is pending;
the already-started query continues another iteration. The timer posts that
bit before submitting work, without running the workqueue. Timing, event and
sleep results are supplied, not a simulation of the RTOS scheduler. These
cases neither establish the observed packet traffic's origin nor explain
zero thermal limits by themselves. The JSON records assumptions, stop points,
bus topology and calls. Live cause and recovery remain unestablished.

## What the live probe observes

Loading the module performs one roughly eight-second capture, then exposes
only cached root-readable attributes. Re-reading those files sends no further
firmware requests. There are no writable attributes or module parameters.

- Initial and final EC source budgets: package PL1/PL2, system PL1–PL4, in mW.
- Initial, final and 24 intermediate observations of the published time mirror.
- Mailbox status before/after each sample, the observed opcode, and whether
  the first eight bytes changed since the preceding sample. Packet payloads
  and unrelated SRAM are not exposed.
- Version checks before and after each sample, plus two agreeing budget reads
  at each endpoint. A failed check or disagreement aborts without retry.
- Host boot ID, monotonic sample timing and capture time for offline comparison.

The only firmware operation is the traced OEM12 fixed-address read. Allowed
address/length pairs are hard-coded: version `0x06000760/5`, source limits
`0x06000714/24`, time `0x06000788/6`, shared packet SRAM `0x06000800/8`, and
nondestructive mailbox status `0x06000504/1`. There is no arbitrary-address
interface. These EC fields have no `_DSM` map; this separate diagnostic uses
a release-pinned investigation contract, not the hwmon driver's ACPI contract.

No EC command packet is submitted, no event is acknowledged or cleared, and
no power limit, nonsecure mailbox flag, reset state or EEPROM field is written.
In particular, the consuming register `0x06000500` is never read. Firmware and
other Linux drivers can still produce their own background traffic.

**The SoC RESP2 wrapper discards the inner EC read status.** A successful FF-A
call is not proof that every EC read succeeded. The version canaries detect
some failures, not all, even when placed immediately around a sample. Repeated
masked failures can agree. The samples are sequential, can be torn, and can miss
traffic between them; they do not establish request/response ownership. A
static RTC mirror does not prove the physical RTC is stopped.
The firmware reader can also service pending background events after a read;
the observer must not be assumed to leave firmware scheduling unaffected.
The deeper replay additionally shows missing software checks of completion
type, tag and length, and an unchecked deferred GET_PC response. Conditional on
those inputs reaching software, a successful inner read can itself return
stale or mismatched data. Hardware filtering and live occurrence are unknown.

## Build, sign, collect

Run between compute jobs on a validated Spark. The collector requires an
unbound OEM endpoint, no bound Linux client on firmware partition `8003`
(including other UUID endpoints), and at least 5 GiB available memory; it does not stop a
service, unbind another driver, or change system configuration. Build as the
ordinary user from the repository root:

```sh
make -C diagnostics/ec-publication W=1
```

Sign with an already enrolled local key, following the project's
[key guidance](../../docs/installation.md#2-use-an-enrolled-signing-key).
For the existing fan-control key location used during validation:

```sh
sudo mokutil --test-key /root/.local/share/dgx-spark-fan-control/keys/MOK.der
# Continue only if it says the certificate is already enrolled.
# Some mokutil builds return 1 even for that affirmative result.
sudo /lib/modules/"$(uname -r)"/build/scripts/sign-file sha256 \
  /root/.local/share/dgx-spark-fan-control/keys/MOK.priv \
  /root/.local/share/dgx-spark-fan-control/keys/MOK.der \
  diagnostics/ec-publication/dgx_ec_publication.ko
```

Then collect and analyze from the repository root:

```sh
mkdir -p local
umask 077
sudo bash diagnostics/ec-publication/collect.sh > local/ec-publication.txt
python3 diagnostics/ec-publication/analyze.py local/ec-publication.txt
```

The collector loads the already signed module, copies its cached output,
unloads it, and verifies that its module and endpoint binding are gone and
the boot ID is unchanged. It also attempts removal on error or interruption.
Check the exit status. A failed removal is an error, never a successful capture.
An abrupt machine failure or uncatchable process termination can bypass cleanup.
The preflight client check is a point-in-time observation, not a cross-driver
lock; it cannot stop a later binding or firmware's own background traffic.

`analyze.py` is offline and unprivileged. It refuses incomplete captures,
missing cleanup evidence, invalid version/time fields, implausible budgets,
missing or reordered samples, and invalid timing. Exit 0 means the input was
parsed, **not** that the machine is healthy; exit 1 means invalid input. Its JSON
assessment distinguishes the observed fault pattern from publication progress
and inconclusive evidence. It does not claim recovery or GPU performance.

The current collector writes v2 records; the analyzer still accepts v1 records
and reports their weaker version-check coverage. To compare two completed v2
captures from the same host boot:

```sh
python3 diagnostics/ec-publication/analyze.py local/after.txt --baseline local/before.txt
```

The comparison rejects different/missing boot IDs, reversed capture dates,
and overlapping or reversed monotonic sample intervals. It distinguishes a
repeated publication fault from newly observed publication progress. A host
boot ID does not detect an EC reset or service restart. Endpoints cannot prove
continuous behavior between captures, and publication progress still needs
SPBM and workload validation before being called recovery.

Keep captures and built modules out of commits. Only original source, synthetic
test data and summarized findings belong in the repository. The earlier
full-window and active-command prototypes remain private research artifacts;
they are not part of this interface. See the
[investigation](../../docs/no-restart-recovery.md) for their findings and limits.

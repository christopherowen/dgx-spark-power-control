# Investigating recovery without restarting

**No recovery without a host restart has been demonstrated.** The EC firmware
can publish power limits during normal operation, but we have not identified
a host-accessible command that safely restores that path in the observed
fault state. This is an open investigation, not a claim that live recovery is
impossible. The power-control driver currently provides diagnosis only for
this fault.

## Observed fault and comparison

Read-only captures on two P4242 Sparks, 2026-09-26, distinguish these states:

| Measurement | Affected unit | Healthy control |
| --- | --- | --- |
| EC-visible package PL1/PL2 | 0/0 | 140/142 W |
| EC-visible system PL1–PL4 | All zero | 231/244/257/265 W |
| Effective package limits | 20/20 W | 140/142 W |
| Effective system PL1/PL2 | 30/30 W | 231/244 W |
| EC-published time | Same old valid timestamp across captures | Advances |
| Loaded GPU clock, earlier matched inference run | Mean 554.5 MHz | Mean 2494.95 MHz |
| NVIDIA clock-event reasons | Zero | Zero |

The EC source reads used a separate fixed-address diagnostic probe. This
repository's hwmon driver reads the SoC's SPBM copies only. A firmware RESP2
wrapper discards the inner EC-read error code: FF-A success alone does not
prove an EC read succeeded. Repeated individual-word/block agreement, valid
nonzero version/time bytes and the healthy control support the finding.
The time field is a software mirror, so its staleness does not prove that the
physical RTC or every EC task is stopped.

These observations point to EC initialization/state or publication progress,
upstream of merely copying good limits into SPBM. The precise trigger remains
unresolved. An error-history bit of `0x0002` traces to OOB_RST_WARN history,
not an identified brownout or causal power-supply fault.

The repository driver was subsequently loaded with `read_only=1` on both
units. Its `diagnose --json` result distinguished the fallback pattern from
published healthy limits. Both modules were unloaded, with unchanged boot IDs.
Inference was already stopped before those later captures; they do not
validate operation during an active workload. See [validation](validation.md).

## Live command-handler and worker probes

After the owner stopped inference, two fixed diagnostic getters were tested
on the affected unit using temporary research modules, not the hwmon driver.
Neither command sets time, power limits, EEPROM settings, or reset flags.

- Cached-time getter `05 01` returned the expected `05 01` response and the
  same old timestamp, 07:31:32 UTC. This demonstrates a responsive EC command
  handler; it does not demonstrate periodic RTC publication.
- Worker status getter `05 09` is dispatched through the RTC thread. Its
  traced branch writes the constant `b7` into response byte 2. The live
  generic-EMI completion instead returned `12 00 00`, so the expected worker
  acknowledgement was **not** established. No retry was sent.

The SoC generic-response dispatcher at `0x9397940c` checks its generic-pending
flag before inspecting the response opcode. While that flag is set, it routes
the response through `0x939791c0` and copies the shared EC buffer without
matching the request opcode. This makes an unrelated response or shared-buffer
overwrite plausible, but the capture alone does not distinguish these causes
or prove the cause of the original latch. A successful generic completion
must not be treated as a successful repair acknowledgement.

There is a concrete matching source of background traffic in the SoC firmware:
SCI event `0xe2` dispatches through `0x93972454` to `0x93978440`. That routine
constructs `12 00 00 00` followed by four bytes from `0x1c23880c`, the verified
`SPBM_PWR_AVG_EWMA_S_SYSPL1_OFFSET` register. It submits the eight-byte packet
through `0x93979030`. The EC's opcode-`0x12` branch at `0xc4c30` consumes that
power measurement and acknowledges through the common response path. Its
unchanged three-byte prefix matches the unexpected generic response.

The common sender checks the hardware mailbox's busy bits but does not check
the generic-pending flag. Combined with the response dispatcher above, this
provides a specific competing-transaction path to investigate. It is static
evidence of a matching producer, not a runtime trace proving which transaction
supplied the captured bytes. It also does not establish the original latch's
cause. Excluding Linux clients alone cannot exclude this firmware traffic.

The unexpected response was preserved before clearing only its nonsecure
output-available flag, using an exact full-buffer match. No request was
retried, and no EC event register was cleared by Linux. The shared transport
ended with pending and output-available both zero. All research modules were
unloaded and the host boot ID remained unchanged. The query prototypes and
raw responses remain under ignored `local/`; they are not supported commands.
An independent fixed-address read afterward still found all six source
budgets zero and the same stale time mirror. Neither getter repaired the fault.

The next transport investigation must account for unrelated traffic and prove
request/response ownership before attempting a producer-recovery command.

### Deferred request lifetime

The EC has a second ownership gap along the RTC-worker path. Callback
`0xcc830` reads the incoming command through `0xc6770`, then dispatches through
`0xc4a88`. For an opcode-5 request other than the cached-time branch, it posts
event 1 to the RTC thread without making a private copy of the request packet.
The worker later calls `0xc49b0`, ignores its return, and reads the subcommand
from the same shared packet SRAM at `0x118c00`.

The helper's mutex protects its mailbox-busy polling, not the lifetime of the
deferred request: it releases the mutex before the caller reads the packet.
Its timeout is `0x18000` ticks, approximately three seconds at the traced
32,768 Hz tick rate. The separate response helper also does not preserve the
request between dispatch and worker execution. This establishes where a
competing packet can interfere; it does not prove which race occurred in the
earlier `05 09` experiment. Adding opcode matching only in Linux would detect
some mismatches but would not repair this firmware ownership problem.

## Passive publication capture

A later capture submitted no EC command packet. It read only SRAM mirrors,
the current packet SRAM and nondestructive mailbox status, without reading the
consuming event register. Over roughly 7.36 seconds on the affected unit,
the packet changed while all six source budgets and the time mirror stayed
unchanged. The packet opcodes included `0x10`, `0x11`, `0x12` and `0x14`.
The healthy control published nonzero budgets and advancing time.

The repeatable, narrow version is now in
[`diagnostics/ec-publication`](../diagnostics/ec-publication/README.md).
It is an optional temporary probe in this repository, not a second power
controller. It exposes named observations instead of the original full SRAM
dump. The collector performs compatibility checks, unloads the probe, and
verifies cleanup and an unchanged boot ID. Its offline analyzer refuses
incomplete evidence. Neither it nor the hwmon driver offers active EC commands.

| Repository probe, 2026-09-26 | Affected unit | Healthy control |
| --- | --- | --- |
| Initial/final package sources | 0/0 W | 140/142 W |
| Initial/final system sources | All zero | 231/244/257/265 W |
| Published time | 07:31:32 UTC throughout | 10:41:44 → 10:41:52 UTC |
| Sampled packet changes | 7 | 7 |
| Status before/after each sample | `0x08`; busy bits clear | `0x08`; busy bits clear |
| Probe unloaded / boot ID unchanged | Yes / yes | Yes / yes |

The observations make a complete EC/mailbox stall unlikely. They do not show
that every task is running, that no transaction is outstanding between samples,
or that the physical RTC advances. The inner-read-status limitation still
applies. Inference remained stopped; clocks were not retested under load.

### Initialization and publication gates

The traced producers have different dependencies:

| Producer | Traced dependency | What remains unknown |
| --- | --- | --- |
| RTC mirror | Thread `0xc188c` initially waits for bit 1 of event object `0x11a044`; a one-second timer posts its periodic event. Reader `0xc14d4` uses logical I2C bus 0, address `0x32`. | Current task/event state, whether reads continue, and physical RTC progress. |
| Package limits and thermal policy | Thermal thread `0xc3a28` requires power state 2, initialization flags and publication enable. Its sensor initialization uses logical I2C bus 2 before the nominal-limit publisher. | Whether this specific task reaches publication and which gate, if any, prevents it. |
| System limits | PLTRST work `0xc2760` initializes the shared window if needed, signals event `0x40`, and calls publisher `0xc3e24`. A conditional boot-ready wait has a five-second timeout; its result does not gate the publisher. | Whether this work item reaches/completes the publisher, or a later initialization clears its output. |
| Background power/temperature queries | Query threads initially wait for boot-ready once; their subsequent loops check power state 2 and publication enable. | Activity supports progress on these paths, not the state of the separate producer tasks. |

Window initialization `0xc4820` clears the budget and time mirrors, preserves
selected PD/metadata bytes, clears three thermal initialization flags, and
reads/publishes RTC time once. The preserved-byte table does not include
budgets or RTC time. A valid time after initialization therefore does not show
that the periodic worker subsequently ran.

The power-on wait at `0xc1b08` is asymmetric: its polled-PLTRST branch posts
boot-ready bit 1, while its event-`0x40` branch returns without that post.
This is a candidate lifecycle race, **not an established cause**. In particular,
the system publisher's bounded wait means a missing boot-ready bit alone
cannot explain indefinitely zero system budgets if that work item completes.
The active query traffic also argues against assuming all tasks are suspended.
RTC and thermal initialization use different logical I2C buses, so a shared
bus fault has not been established either.

These distinctions leave selective task progress, work-queue progress and
shared-window reinitialization as investigation targets. No host-exposed,
non-disruptive control for those internal states has yet been established.

## Firmware paths checked

These are interpretations of distributed release capsules matching the
reported EC 3.5.8 and SoC 2.155.11 versions, not live flash readbacks or a
vendor-supported ABI. The original baseline repository commit is
`490a3c6e5fd16ddbf825dd9dac2a2c625f47e726`.

| Candidate | Result |
| --- | --- |
| Remove an OS cap with `automatic` | OS slots were already zero. Clearing them cannot recreate EC data. |
| Force SPBM recomputation | The traced transfer rejects all-zero EC values. A recomputation request alone is not a demonstrated repair. |
| EC package publication | Routine `0xc3d4c` publishes nominal values, with retry behavior in the thermal task. Another runtime path, `0xc3394` → `0xc3d84`, is gated by a GPIO and publication flags. Runtime publication exists; a safe host trigger for the stuck state has not been established. |
| EC system publication | `0xc3e24` is called by the PLTRST-related work at `0xc2760`. Replaying reset signals or power-state transitions is not a verified non-disruptive trigger. |
| Resume producer tasks | The traced resume call at `0xc2230` belongs to power-on sequencing. It is not an independently exposed live-repair API. |
| Host subcommands `0x0f/0x12` and `0x0f/0x13` | Control the thread at `0x1193d0`, identified by its initializer as `pwr_btn_thread_id`. They do not resume the SMC or RTC producer threads. |
| Host subcommands `0x0f/0x1c`–`0x1d`, `0x1f`–`0x21` | Update flags and schedule work associated with EEPROM settings at offsets `0x10`/`0x11`; not established producer-recovery controls. None were sent. |
| EC software reset | An arming command reaches SYSRESETREQ after power-down state 5. This still requires host power-down and is outside the no-restart objective. Not executed. |
| Populate limits manually | Would conceal the missing publication and leave other EC health unresolved. No EC-slot, PID, budget or register-override interface was added. |

The SoC also has a transfer robustness weakness: its SCI path can clear a
power-limit event even after the transfer handler returns early. That remains
worth fixing, but cannot alone explain the EC-visible zeros and stale time.

## Required evidence for a live repair

### Read fidelity and observer effects

The legacy FF-A response path is not a Linux-accessible replacement for the
status-discarding RESP2 path: the SoC dispatcher at `0x9395f518` admits source
partition `0x8002` and consumes a shared-memory protocol. No legacy request
was sent from Linux.

The OEM12 reader at `0x9396c80c` divides reads into 64-byte chunks and a
remainder, returning early on a failed chunk. This suggested placing the
version canary after the budgets in one 81-byte request. On the healthy
control that experiment failed its version check (`-EBADMSG`). At the same
time the fan client's transactions began returning firmware status 5. Its
daemon exhausted retries, failed automatic restoration, and exited; silence
after six seconds was **not recovery**. A later original-size version read
also returned zeros and was refused. Both diagnostic modules unloaded and
the host boot ID stayed unchanged. The cause of the transport failure is not
established; this is treated as an investigation-induced incident. The larger
request was not repeated and was never sent on the affected unit. It is
excluded from the repository implementation, and live EC experiments stopped.

The retained probe uses only the original five address/length pairs. It now
checks the version before and after each sample, requires two budget reads to
agree at each endpoint, aborts without retry on disagreement, and preserves
the actual probe failure code. These checks strengthen evidence but cannot
recover the discarded inner status or rule out repeated masked read failures.
The collector now also refuses any bound Linux client on firmware partition
`8003`, even when that client has a different endpoint UUID. Previously it
checked only the OEM endpoint; the fan endpoint shares the same partition.
This preflight cannot serialize firmware background producers or a later
Linux binding. It is an additional refusal condition, not proof of isolation.

After reading, the SoC routine calls `0x9396c0a0`, which can service pending
background events. Consequently, packet activity during passive collection
does not prove that the same timing or traffic would occur without an
observer. No Linux EC command packet or event ACK is submitted by this probe.

The affected unit's kernel boot log reports EFI RTC time **07:31:32 UTC**, the
same timestamp still present in the EC mirror hours later. This supports
investigating startup publication progress; it does not establish a particular
deadlock, nor justify treating a direct RTC query as a harmless next step.
The existing RTC-worker query already demonstrated unsafe response ownership.

V2 captures include a host boot ID and can be compared offline with
`analyze.py after.txt --baseline before.txt`. The comparison verifies host
boot identity and monotonic ordering, while explicitly distinguishing
publication progress from complete recovery. It cannot exclude an EC reset
or infer continuous behavior between captures.

### Existing fan-control recovery

`dgx-spark-fan-control` already implements a proven rebootless recovery for a
**stale relay-pending flag with a readable, idle physical mailbox**. Its
[0.1.3 implementation](https://github.com/christopherowen/dgx-spark-fan-control/blob/deb2ea155f6698b769ff4977dea2119e3b32f460/kernel/dgx_ec_fan_control.c)
matches the installed source on both units (SHA-256
`e78e1178ab76732b46ebbe1b56cc03244951d3b2aa48c95cc84b9bd5b2c7f8a6`).
It holds the existing fan owner's mutex, checks a plausible RTC and recognized
response with two idle status reads, then permits one operation-4 floor read
despite cached pending. It verifies the returned physical response and floor
ownership. It does not reset the EC or blindly repeat a setter.

The reference unit's current boot contains **six successful automatic
recoveries**, most recently at 10:41:51 CEST, before the 13:20 transport
incident. That establishes that this method was installed and working for its
intended fault; rebootless transport recovery was not wholly unexplored.

| Observed condition | Does this method apply? |
| --- | --- |
| Packet polls stay at state `2`, while physical status is readable and idle | Yes, subject to the driver's additional response and ownership guards; demonstrated earlier on the reference unit. |
| Submit returns secure status `0x05` / `-EIO`, and even the fixed version read fails | No demonstrated recovery. The current reference-unit incident follows this path. Recovery is entered for `-ETIMEDOUT`, not every submission error. |
| EC responds, but budgets are zero and periodic time publication is frozen | No demonstrated producer repair. This is the affected unit's original condition. |

The affected unit's fan probe had already received floor `0x0000` at boot and
refused to bind because it would replace an existing floor. This is not a
logged stale-pending timeout. In the EC image, operation 4 branches from
`0xc3900` to `0xc3998`, reads mirror offset `0x192`, places the value in the
reply, and acknowledges through `0xc4a4c`. It does not directly invoke the
package/system publishers or producer-thread resume routines.

The older operator helper is pinned to kernel `6.17.0-1029-nvidia` and private
driver layouts for versions 0.1.0/0.1.1; it is not applicable to these loaded
0.1.3 drivers on kernel 7.0. At **13:36:18 CEST**, one `cur_state` read through
the installed reference-unit driver returned `EIO`, with operation 4 submit
status `0x05` again. Its ordinary pending preflight reached submission rather
than the timeout-recovery branch. The OEM endpoint was unbound, no diagnostic
probe was loaded, and the host boot ID stayed unchanged. No helper, forced
retry, floor write or service restart was executed. The installed recovery
guards were left intact, and no read was repeated.

### Criteria for an active experiment

The next useful mechanism would resynchronize host/EC initialization or resume
the existing producer tasks, while keeping the host and EC running. Its full
call path must be traced before sending it; a nearby command number or a name
resembling “resume” is insufficient. Direct EC thread state or a firmware trace
would help distinguish a task waiting for initialization from a stalled work
queue or an incorrect power-state observation.

Before any further active query, the generic transport needs an ownership
solution covering both firmware background senders and deferred EC workers.
The passive capture can measure the publication symptoms without depending on
that generic request path, but cannot reveal EC thread stacks or prove which
internal task is waiting. Those are the remaining evidence gaps, not a reason
to expose guessed repair commands through the repository.

A successful experiment must preserve the host boot ID and workload process
identity, restore advancing EC publication, restore valid EC budgets and their
SPBM copies, and recover clocks under the same workload. A faster clock alone,
a successful FF-A return, or a populated OS override is not enough to establish
that the firmware fault was repaired. No reset or budget override should be
hidden behind a diagnostic command.

## Preventing recurrence

No prevention mechanism has been validated. The observations are consistent
with incomplete initialization or stalled publication, but do not establish
which event creates that state. A prevention change needs a reproducible
trigger and before/after evidence that the producer keeps publishing valid
budgets and advancing time. Detecting the fallback with `diagnose` can help
capture that evidence; detection alone does not prevent the latch.

## Reproducibility and source boundaries

EC virtual mapping: capsule file `0xd9f` → `0xc0000`.
SoC eSPI mapping: capsule file `0xa08f0f` → `0x93942000`.

| Input | SHA-256 |
| --- | --- |
| `ec_fused.cap` | `ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82` |
| `socfw.cap` | `0985b848b1708421a399935b7f9b1afb5469588db6f26bd2953c6013f3db70ff` |

Keep capsules, disassembly, raw captures and signing material out of Git.
Local evidence for this work is under ignored `local/`, with the preceding
investigation retained separately. Only findings and original implementation
code belong in the repository. [NVIDIA's release notes](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)
list these EC/SoC versions; they do not establish a fix for this observed fault.

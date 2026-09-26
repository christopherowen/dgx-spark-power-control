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

The next useful mechanism would resynchronize host/EC initialization or resume
the existing producer tasks, while keeping the host and EC running. Its full
call path must be traced before sending it; a nearby command number or a name
resembling “resume” is insufficient. Direct EC thread state or a firmware trace
would help distinguish a task waiting for initialization from a stalled work
queue or an incorrect power-state observation.

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

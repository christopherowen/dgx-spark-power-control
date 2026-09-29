# dgx1 EC transport incident, 2026-09-26

The investigation caused an operational regression on the reference machine:
EC transactions began failing and the fan-control daemon could no longer read
or restore its floor. Treat dgx1 as affected, not as a healthy control. Physical
hardware damage has not been established. The current fan floor is unverified.

## What happened

Times below are CEST. Source archives, build checksums and raw logs are retained
under ignored `local/`; proprietary firmware and machine logs are not published.

| Time | Evidence |
| --- | --- |
| Before 13:20 | The control had valid power budgets and advancing EC time. The installed fan driver had recovered six stale-pending incidents during this boot, most recently at 10:41:51. |
| 13:20:00 | An experimental OEM12 request read 81 bytes starting at `0x06000714`, intended to obtain budgets and a trailing version canary together. Its version check failed. Fan requests began returning secure status `0x05` / `-EIO` in the same second. |
| 13:20:02–07 | The fan daemon retried, failed to restore automatically, and exited with status 69. The subsequent silence was daemon exit, not recovery. |
| 13:23:37 | A separate, original-size five-byte version read returned zeros and was refused. Unloading the experimental module had not restored communication. |
| 13:36:18 | One normal floor read through the existing fan owner returned `-EIO`, again with operation `0x04`, status `0x05`. Its timeout-based stale-pending recovery did not run. |

The exact first-attempt source archive shows that the failed version check
returns before the proposed 46-byte time read or sampling loop. There was one
81-byte OEM request; we do not know which underlying operation first failed.
That larger request was never repeated or sent to dgx3. All temporary research
modules were unloaded; recorded host boot IDs did not change.

No Linux setter or event acknowledgement was sent by the experimental probe.
Nevertheless, a read request drives the shared eSPI controller and changes
transport state. Its firmware path can also service background events. The
experiment incorrectly treated an accepted wrapper length and read-only
address range as sufficient evidence of transport safety. It also lacked a
partition-wide client exclusion check. Shared clients are a risk factor;
their presence does not establish that a concurrent-client race caused this
incident.

## What the firmware replay establishes

[`replay_firmware_reads.py`](../diagnostics/ec-publication/replay_firmware_reads.py)
executes the original AArch64 read wrapper and initialization prefix in
emulated memory. It verifies the SoC 2.155.11 capsule SHA-256, stops before bus
operations, injects explicit success/failure results, and refuses unexpected
execution. It contains no hardware interface or firmware bytes.

The wrapper at `0x9396c80c` calls lower reader `0x93969c60`:

- First, 64 bytes at `0x06000714`; then, if successful, 17 at `0x06000754`.
- A failed chunk returns `-1`, does not retry, and skips wrapper housekeeping
  at `0x9396c0a0`.
- Failure of the second chunk leaves the first 64 output bytes copied.
- When both chunks succeed, housekeeping runs and its error can still become
  the wrapper's result. Successful data transfer and successful housekeeping
  therefore need to be distinguished.

Lower reader `0x93969c60` also has its own cleanup through `0x9396035c`.
Skipping wrapper housekeeping does **not** mean all error cleanup is missing,
nor prove that skipping it causes a persistent wedge.

An initial alignment hypothesis needs qualification. The first chunk crosses
a 64-byte address boundary, but the initialization prefix requests channel-0
configuration `0x7115`: a 4096-byte maximum read and a separate 64-byte maximum
payload. The [eSPI specification, sections 4.1.3 and 6.2.1.4](https://cdrdv2-public.intel.com/841685/841685_ESPI_IBS_TS_Rev_1_6.pdf)
distinguishes these fields. The wrapper's 64-byte chunk size must not be
mistaken for the configured maximum read size. Both proposed reads fit a
4096-byte boundary. Completion alignment/length handling still merits review;
this is not an established malformed-request diagnosis. The replay shows what
initialization requests, not the live hardware's accepted configuration.

The nine fixed read cases and initialization-prefix check passed offline with
Unicorn 2.1.4. Physical responses, timing, interrupts and housekeeping internals
are simulated. This replay does not reproduce or explain the persistent fault.

## Lower transport replay

[`replay_firmware_transport.py`](../diagnostics/ec-publication/replay_firmware_transport.py)
extends execution through the original lower reader, transaction routine,
FIFO reader, error cleanup and background handlers. Only raw register
accessors, logging, delays, initial controller programming and RAM memcpy
are replaced. It runs in the same hash-pinned image with an instruction budget
and refuses unknown register accesses. Eleven transport scenarios and two
general-configuration cases passed with Unicorn 2.1.4.

The controller model supplies completion bytes and response/status registers;
it is not a hardware emulator. There is no asynchronous interrupt interleaving,
CRC or hardware filtering, DMA, or unrelated channel traffic. Empty FIFO reads
return zero by explicit assumption. Delays are skipped, so poll counts are
evidence about iteration limits, not measured elapsed time.

### Isolated errors do not create a lasting software latch

Supplying initial response `0x03`, `0xff`, or a command-done timeout causes the
81-byte wrapper to return `-1` after its first 64-byte request. A later healthy
five-byte transaction succeeds without resetting emulated software memory.
The command timeout takes 5001 polls. Cleanup restores interrupt settings via
`0x9396035c`; the timeout hook at `0x9395fda4` is a no-op. No controller reset
or FIFO flush appears on these executed error paths.

This narrows the question to an additional persistent controller/EC condition,
other software paths, or concurrency. It does not eliminate those possibilities
or establish that a particular physical error occurred.

### Successful returns can contain old receive-buffer bytes

The manual-FIFO reader copies the requested length from receive-buffer offset
3 without validating the completion header's type, tag or length. After a
synthetic five-byte read seeds `01 02 03 04 05`, the following cases return 0:

| Supplied controller input | Five bytes copied |
| --- | --- |
| Successful completion with length 1 and payload `ee` | `ee 02 03 04 05` |
| Unsuccessful completion header, no payload | `00 02 03 04 05` |
| Successful five-byte completion with a different tag | Supplied wrong-tag payload |
| Nonfatal response and empty FIFO | `00 02 03 04 05`, with one empty-FIFO read |
| Deferred read followed by fatal GET_PC response | Prior `01 02 03 04 05` unchanged |

The zero in two cases comes from modeled FIFO padding/empty-read behavior;
that byte value is not asserted for real hardware. The deferred path verifies
the transaction routine's return but does not verify GET_PC's response byte
before copying. Actual controller filtering could prevent some of these
inputs from reaching this path. The result establishes a software validation
gap, not that either machine produced these exact inputs.

### Conditional persistent failure and modeled drain

One scenario preserves the same emulated software state throughout:

1. A request receives DEFER. Its completion is not ready during 1001 GET_STATUS
   polls, and the wrapper returns `-1`.
2. The completion becomes available later. **The target model assumes one
   request slot held until that completion is consumed.** It reports PC_AVAIL
   set and NP_FREE clear.
3. The original reader issues a new PUT_NP without waiting for NP_FREE. The
   model responds with fatal error. Five subsequent reads fail: a five-byte
   version read, an aligned 16-byte read, one byte at a 64-byte boundary, the
   original 17-byte tail, and the original first 64-byte chunk. Each issues
   only a new PUT_NP; none reaches GET_PC to consume the pending completion.
4. The original housekeeping `0x9396c0a0` and alert handler `0x9396c308` leave
   this completion pending. They handle other channel bits but have no
   explicit PC_AVAIL branch. A further small read still fails.
5. Calling original availability wait `0x9396570c(4)` and GET_PC `0x93969b1c`
   drains the modeled completion. The following small read succeeds. No
   emulated controller, EC, host or software context is restarted.

This is a causal example **within an assumed target model**, not a reproduction
of the physical incident. [The eSPI specification, sections 3.2, 3.4 and 3.8](https://cdrdv2-public.intel.com/841685/841685_ESPI_IBS_TS_Rev_1_6.pdf)
defines request flow control and deferred completions. It does not establish
this unit's queue depth, credit lifetime, or recovery after a fatal response.

The example also assumes GET_STATUS does not append a completion. The replay
of the initialization prefix preserves General Configuration bit 30: starting
clear produces `0x90000000`; starting set produces `0xd0000000`. It does not
force response modifiers on. Per section 6.2.1.3, the bit defaults clear. Live
configuration and later writes have not been verified. Automatic consumption,
a free request slot, or a channel latched after fatal error would change the
model's outcome.
In particular, the modeled target continues accepting GET_PC after reporting
fatal errors; that behavior has not been established for this hardware.

The direct-call audit found GET_PC only in the deferred Memory Read32 and
Memory Read64 paths. No independently callable Linux drain operation has been
established. A scan for raw function pointers and nearby ADRP/ADD address
construction found no additional references; this is not a complete proof
about indirect calls. The reset routine's observed caller is initialization, which also
handles PLTRST; re-running that sequence is not a scoped drain.

### EC-side cross-check

The EC image's device table at `0xcfc58` points to configuration `0xd09e4`,
with eSPI bases `0x400f3400` and `0x400f9c00`. Together with the capsule's
Microchip signature marker and SRAM map, these support a Microchip MEC-family
identification; exact silicon and revision have not been attested. The
[MEC172x datasheet](https://ww1.microchip.com/downloads/aemDocuments/documents/CPG/ProductDocuments/DataSheets/MEC172x-Data-Sheet-DS00003583.pdf)
is consistent with that map but refers to a separate eSPI block specification
for the complete register set. It does not establish the modeled queue policy.

The image's peripheral ISR at `0xc9098` reads base+`0x114`, clears bit 16,
handles channel-enable and bus-master changes, then clears the interrupt.
There is no explicit drain/reset in its bit-16 error branch. Its structure
matches the [upstream Microchip eSPI driver's error handler](https://github.com/zephyrproject-rtos/zephyr/blob/3e7672a71bd954ca46c38c99d8d8f68a0fb23090/drivers/espi/espi_mchp_xec_v2.c#L761),
which identifies that bit as a bus error. This is a source correlation, not a
claim that the distributed image exactly matches upstream or that hardware
does not perform additional recovery internally.

## Next evidence to obtain

1. Establish whether the affected transport has a pending peripheral completion,
   whether NP_FREE is clear, and the actual response/error state. The current
   Linux status-5 result loses this detail. Existing kernel logs contain no
   discriminating controller state. Seek an existing firmware trace/export
   before considering a new operation; direct secure MMIO is not a substitute.
2. Determine the real target's credit policy and handling of late completion
   and fatal responses. A bus trace, matching controller documentation or
   vendor firmware instrumentation could validate or falsify the model. Do
   not infer these conditions from the larger-read trigger alone.
3. Establish whether the firmware exposes a narrowly scoped way to drain or
   recover that transport state while preserving EC power, thermal tasks and
   fan ownership. Trace every side effect before proposing live use. A bus or
   channel reset is not automatically harmless or equivalent to task recovery.
4. Keep dgx3's original publication failure separate. Its command path was
   responsive while budgets were zero and time publication was frozen. Review
   startup events, thermal/RTC task gates and shared-window reinitialization;
   repairing dgx1's transport would not itself establish a dgx3 fix.

The existing fan recovery covers stale pending with a readable, idle physical
mailbox. It does not establish recovery from the present status-5 failures.
Do not bypass its guards, retry the wider request, unbind existing clients to
pass the diagnostic collector, or access the secure controller through Linux
MMIO. No further live EC transaction was made for this offline investigation.
There is not yet a demonstrated **hardware** recovery without restarting for
either current fault. A modeled drain is not a live repair procedure, and there
is no evidence that a cold power cycle is the only possible recovery.

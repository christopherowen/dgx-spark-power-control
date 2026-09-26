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

## Next evidence to obtain

1. Trace the lower reader, transaction routine `0x93961864`, completion parser,
   and cleanup `0x9396035c` offline. Identify which timeout, rejected completion,
   or retained controller state could make later small reads fail. Review
   completion length/alignment against the requested channel configuration.
2. Extend the offline replay below the current stub, with explicit controller
   and completion models. Require a causal example: the first failure must
   leave state that explains a subsequent failed request, then a modeled repair
   must restore it. Injecting an error alone is not a root-cause reproduction.
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
There is not yet a demonstrated recovery without restarting for either current
fault, and there is no evidence that a cold power cycle is the only possible
recovery.

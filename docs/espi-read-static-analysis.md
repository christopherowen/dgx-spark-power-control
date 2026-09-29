# Static analysis of the chunked eSPI read path

Offline, read-only analysis performed on **2026-09-26** following the
no-restart-recovery investigation. It answers two questions that
investigation left open, using only distributed firmware capsules and
existing logs: which reads through the SoC secure partition's chunked
reader can exceed 64 bytes, and whether a failed read's error path
releases every lock and flag it holds. No module was loaded, no FF-A
request was issued, no EC or service state was touched, and no hardware
was exercised.

## Image provenance

Both capsules were re-downloaded from the public LVFS channel that
`fwupdmgr` uses and verified byte-for-byte against the SHA-256 values
pinned by the investigation, so every address below refers to the exact
analyzed images:

| Capsule | LVFS component | SHA-256 |
| --- | --- | --- |
| `ec_fused.cap` | `com.nvidia.dgx.spark.ec.firmware` | `ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82` |
| `socfw.cap` | `com.nvidia.dgx.spark.socfw.firmware` | `0985b848b1708421a399935b7f9b1afb5469588db6f26bd2953c6013f3db70ff` |

Virtual mappings used: EC capsule file offset `0xd9f` → EC VA `0xc0000`;
SoC capsule file offset `0xa08f0f` → SoC VA `0x93942000`. Per repository
policy, capsules, disassembly, and raw captures are not committed; only
these findings are.

## The chunking rule

The OEM12 reader `0x9396c80c(address, length, dest)` splits reads into
64-byte chunks plus a remainder, as the investigation recorded, with two
additional load-bearing details visible only in the image:

1. **The chunk size is a hardcoded `#64` immediate** (`0x9396c83c`), as
   it is in every sibling routine (`0x9396d2ec`, `0x9396c6d8`,
   `0x9396ce44`). This 64 is a **payload/block constant of the
   chunking family only** — it is not the wire limit, and no routine in
   this family reads a negotiated or configured size. It must not be
   conflated with the transaction layer's configured maximum read of
   **4096 bytes**, which is set up separately in the async transaction
   path (`0x93968bcc` passes `#0x1000` to the setup helper
   `0x9395cca8`). The 64-vs-4096 distinction matters to the trigger
   question: a 64-byte chunk crossing a 64-aligned address is not, by
   that fact alone, an oversized transaction — whether it is illegal
   at all depends on the window/EMI alignment rule, which remains the
   open specification question.
2. **The chunk grid is anchored to the caller's start address**: chunk
   *i* spans `[address + 64·i, address + 64·i + 64)`
   (`0x9396c870`–`0x9396c8a8`). It is not anchored to the EC's 64-byte
   window grid. Any read with `address % 64 != 0` and length ≥ 64 issues
   a transaction that crosses an aligned boundary; a short read crosses
   when `(address % 64) + length > 64`.

For the investigated 81-byte request (`0x06000714`, 24 budget bytes plus
the 5-byte version canary), chunk 1 covers `[0x06000714, 0x06000754)` and
**crosses the aligned boundary at `0x06000740`**; chunk 2 covers
`[0x754, 0x765)` and does not cross. Exactly one crossing transaction was
issued.

On any failed chunk the reader returns `-1` immediately and skips the
post-read background-event service at `0x9396c0a0` (see below).

## Every read over 64 bytes

### Issued from Linux

Across every client revision present on the fleet — the retained
`diagnostics/ec-publication` probe, the earlier limits and passive-state
probes, and the fan driver's fixed OEM reads — exactly one read over
64 bytes has ever been issued: the 81-byte experiment. All other
Linux-issued reads are small, and none can cross a 64-byte boundary:

| Client | Reads (address/length) | Crossing possible | Error handling |
| --- | --- | --- | --- |
| ec-publication retained probe | `0x760/5`, `0x714/24`, `0x788/6`, `0x800/8`, `0x504/1` | No (max span 20+24) | Abort without retry; version canaries |
| ec-state / ec-probe limits probes | 7–9 pairs, all ≤ 24 B within `0x714–0x79c` | No | Abort on transport failure |
| ec-passive-state capture | 33 × `0x600+off/16`, `0x760/5`, `0x800/8`, `0x788/7`, `0x504/1` | No (16-byte steps on a 64-aligned base) | Abort; status bracketing |
| Fan driver 0.1.3 fixed reads | `0x788/6`, `0x504/1`, `0x800/5` | No | Refusal conditions; cooldown |
| Fan driver packet service | Caps 10 B, floor 2 B, telemetry 64 B; the SoC-side packet collect (`0x93976738`) reads 3+64 = **67 B at `0x06000800`** | No — `0x800` is 64-aligned | 100-poll timeout; single recovery |

The last row is a standing control of a narrower kind: reads longer
than 64 bytes traverse this machinery routinely (every telemetry
poll) and succeed, which shows that **length over 64 is not by itself
sufficient to fail**. The one failing read is also the only one ever
issued with an unaligned start. That is a correlation — consistent
with an alignment-related trigger, and with several other
explanations — not an identification of cause; the failing read was
also the only first-of-its-kind request on that endpoint at that
moment. Which variable mattered is not established by this census;
the wire-level question is the window/EMI alignment rule noted above.

### Inside the SoC firmware

All 41 fixed-length call sites of `0x9396c80c` read 1–24 bytes and none
can cross. The sites that can exceed 64 bytes at runtime are:

| Site | Containing function | Length | Notes |
| --- | --- | --- | --- |
| FF-a OEM12 entry | request-driven | ≤ `0x3fd` (RESP2 reply cap) | The probes' path; source of the 81-byte read |
| `0x939753d8` | `0x9397528c` | 1–`0x70` (112) | Direct `0x9396c80c` call |
| `0x93976738` | `0x93976724` | ≤ 258 (packet length + 3) | Packet collect at `0x06000800` |
| `0x93979320` | `0x939791ec` | ≤ 255 | Generic-response consumer, `0x06000800 + offset` |
| `0x9396db48` | — | **fixed 255** | Via second worker `0x9396b704` (wrapper `0x9396d2d4`); runtime address from the state descriptor |
| `0x93971174` | `0x939710b0` | dynamic | Via wrapper `0x9396d2d4` |
| `0x93974518` | `0x939743c0` | dynamic | Via wrapper `0x9396d2d4` |

Whether any of these firmware-internal sites actually crosses depends on
runtime addresses and packet contents; none was observed crossing in the
retained captures.

## The failed read's error path: locks and flags

**The single-chunk workers release everything they take, on every exit.**
Both read workers (`0x93969c60`, used by `0x9396c80c`, and its twin
`0x9396b704`) follow the same bracket: a state-decode helper
(`0x9395fdb8`, not a blocking lock), then busy/ownership flags set at
`0x939a1008+{42,43,47,51}` and `0x939a10f0+56`, a request built at
`0x939a10f0` (`{cmd 2, sub 0xa, length, address}`), submit-and-wait via
`0x93961864`, and a copy from the staging buffer at `0x939a1580`. The
flag-clear block (`0x93969e24`–`0x93969e74`) sits on the common exit
path: the submit-failure return (`-111` at `0x93969d9c`) and the
unknown-status return both branch into it. A failed chunk leaks no flag
at this layer.

Two qualifications:

- The chunked reader's early `-1` return **skips `0x9396c0a0`**, the
  post-read background-event service. That is a missed service, not a
  held lock.
- **The generic-pending flag is the one genuinely latched state in this
  machinery** (`0x939a41b2` bit 0). The packet-command send path
  (`0x93978f1c`/`0x93978fa4`) sets it after a successful send and stores
  the expected opcode and length at `0x939a41b0/1b1`; it is cleared only
  at the top of the response consumer `0x939791c0` — with no timeout and
  no error-path clear. A packet command whose response never arrives
  leaves the flag set, after which the dispatcher `0x9397940c` hands the
  next unrelated response to that consumer without opcode matching. This
  is the mechanism the investigation observed live as the `12 00 00`
  power-measurement response masquerading as the `05 09` worker
  acknowledgement. The flag is set by the packet-send side, not by the
  OEM12 read's error path, and a host reboot restarts the secure world
  and clears it.

## EC-side observations

- `0xc4820` matches the investigation's description in the image: 16
  table-indexed bytes preserved, a 512-byte and a 1024-byte window
  cleared. Annotation corrected on review: `0xc7ce4`–`0xc7d08` read
  **version fields** (four one-byte results stored consecutively at
  `+0x160`–`+0x163`, `0xc4866`–`0xc487a`); the one-shot RTC
  publication goes through **`0xcc6d6` → `0xc14d4`** (`0xc490a`, with
  the reader `0xc14d4` also the RTC thread's own read path).
- The host window maps into EC SRAM **piecewise, not by a single
  base**: host `0x06000600` maps to EC `0x119000` and host
  `0x06000800` to EC `0x118c00` (packet block). Anchors: fourteen EC
  code references to `0x119000` (e.g., literals `0xc1a18` in the RTC
  thread, `0xc3a1c`/`0xc3c20` in the thermal thread) and direct
  references to `0x118c00` in the power-on/RTC region
  (`0xc1708`–`0xc1a14`); the mapping itself is programmed by
  `0xc4820` into peripherals `0x400F3900`/`0x400F3B00` (`0xc25b8`
  configures GPIOs, not this map). This mapping supersedes the single-base
  (`0x118400+`) wording originally used here.
- **Not settled here:** whether the cmd-2/sub-0xa read is executed by EC
  firmware (a mailbox worker whose error path could latch EC state) or
  is served by the eSPI slave hardware window directly, in which case no
  EC-firmware error path exists and lasting damage would live in the
  EC's eSPI controller hardware. The dispatch tree rooted at `0xc4a88`
  is where that answer is; the eSPI peripheral-channel boundary rule
  requires the specification. Both remain open.

## Runtime corroboration (dgx1 journal, same day)

- 12:24:01 — the passive-state capture completed; every read fine.
- 13:20:00 — the 81-byte capture probe failed its version check
  (`-EBADMSG`), and in the **same second** the fan client logged its
  first `status=0x5`. 13:23:37 — a retry's version read returned all
  zeros. The fan daemon then exhausted retries, failed restoration, and
  exited; its service remains failed.
- `status=0x5` appears only in that boot: zero occurrences across the
  eleven prior boots in journal retention, and none in the September 7–9
  24-hour packet traces (their `persistent_pending` event was the known
  mailbox race, not status 5).
- The fan client's earlier-history status-0x5 episodes referenced by the
  investigation predate journal retention on this host and cannot be
  re-examined from it; note that the current fan read sizes (table
  above) cannot cross a boundary at all.

## Assessment against the latch hypotheses

| Hypothesis | Verdict from this pass |
| --- | --- |
| Lock or busy flag left set by the read | Exonerated for the SoC read path (flags bracketed, verified). The generic-pending flag is real but latches on the packet-send side and can misroute responses only — it does not cause raw-link failure; the modeled and observed link failure requires the raw-window layer itself to fail. An EC-side condition surviving reboots remains possible and untested. An EC-firmware latch remains possible and untested. |
| Initialization abort never retried | Nothing in the read path touches the publisher gates the investigation traced (RTC-thread event wait, thermal-thread power-state and flags, PLTRST boot-ready). No static support found at this depth. |
| Latched controller error | Fits the incident unit best: after 13:20 every inner read fails, both OEM12-masked (zeros) and packet-unmasked (status 5). On the originally affected unit the link recovered after a host reboot while the publishers stayed dead, so a controller-level latch there would have to be EC-side — which host resets do not touch and, per report, only removing AC power clears. |

## Next steps, revised after follow-up

1. ~~Identify the EC-side executor for cmd-2/sub-0xa requests~~ —
   **resolved** in [EC read executor](ec-read-executor.md): OEM-12
   reads are served by the EC's eSPI/EMI window hardware; EC software
   executes only the mailbox protocol (dispatch `0xc4a88`).
2. Establish the eSPI peripheral-channel/EMI alignment rule from the
   specification to determine what a crossing transaction means on the
   wire and how each side reports it. Still open; note the 64-byte
   block constant and the 4096-byte configured maximum (above) must be
   argued separately.
3. ~~Check the SoC submit/wait layer for timeout behavior~~ — the
   modeled side is **substantially completed by the transport replay**
   of the submit/wait and completion machinery. The remaining question
   is not static: **whether the physical hardware follows the modeled
   completion/credit behavior** (see O1 in
   [Concurrency analysis](soc-concurrency-analysis.md)).

All three are offline work against the pinned images. Nothing in this
analysis argues for exercising any unit; live reproduction remains
excluded.

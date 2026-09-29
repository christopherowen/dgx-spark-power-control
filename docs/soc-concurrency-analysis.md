# Concurrency analysis of the SoC eSPI read and mailbox paths

Static follow-up to the [eSPI read static analysis](espi-read-static-analysis.md).
Scope: every owner of the shared transmit/receive state in the SoC
secure partition's EC service — the chunked read/write machinery, the
EMI mailbox protocol, the housekeeping service, and the
generic-response dispatcher — and whether any single failed exchange
can leave state that produces **persistent** failure. Pinned images as
before (`socfw.cap` `0985b848…`, VA base `0x93942000`; `ec_fused.cap`
`ab21ddb0…`, VA base `0xc0000`). An executable abstract model of the
resulting state machines is
[`scripts/emi_interleave_model.py`](../scripts/emi_interleave_model.py);
its scenario output is quoted below.

## Shared state inventory (every owner, with addresses)

| State | Address | Set by | Cleared/read by |
| --- | --- | --- | --- |
| Worker mode flags | `0x939a1008+42` (sync), `+43` (async) | every transaction submit (`0x939672xx`–`0x939689xx` chain sets `+43`; sync paths set `+42`) | save/restore bracket in workers `0x93969c60`/`0x9396b704`; cleared on **all** exits (`0x93969e24`–`0x93969e74`) |
| Async-in-flight | `0x939a1008+29` | async submit `0x93968da8` (set), unconditional clear `0x93968de0` after helper `0x939645a4` returns — success or error; other writers (`0x93957644`, `0x9395799c`, `0x93970420`) are bulk byte-range resets | status-decode wait `0x93960658` (`[obj+29]` selects the async branch) |
| ESPIM status words | `0x939a1008+0x70/0x74` (172/180), `+0xb0/+0xb4` (176/180) | transaction completions | wait function `0x93960658` (bits 2/7/12/17/22 of the decoded word) |
| Staging buffer | `0x939a1580` (+3 header) | eSPI completions write received data | workers copy out (≤ `0x3fd`/`0x400` caps at `0x9396c8f0`-family checks) |
| Generic-pending | `0x939a41b2` bit 0 | packet senders after successful send (`0x93978fc0`); expected opcode/length stored at `0x939a41b0/1b1` | **only** the response consumer `0x939791c0` (`0x93979204`), at entry, with no opcode match |
| Mailbox busy bits | `0x06000504` bits 0–1 (EC SRAM) | EC sets while processing; SoC doorbell write (`0x9397913c`) | send engine checks before send (`0x939790b4`); refusal code `0xa`, not 5 |
| Packet block | `0x06000800` (EC SRAM) | SoC senders (command blocks); EC responses and unsolicited producers (SCI `0xe2` → opcode `0x12` via `0x93978440`/`0x93979030`) | consumer `0x939791c0` copies, then retains until overwritten; `0x06000802 == 0x80` is the EC's done mark |
| Post-read service | `0x9396c0a0` | — | runs after every *successful* chunked read; **skipped** on chunk failure (early return at `0x9396c8bc`) |

Serialization: the partition exposing these endpoints has **one
execution context** (established in the investigation; consistent with
the state object being plain bytes with no spinlocks). All
interleavings therefore happen at yield points: FF-A message
boundaries, IRQ/completion callbacks (the `esvc_virq` layer), and the
`msleep`-style waits inside the ESPIM driver. There is no preemption
inside a single transaction; the race surface is exactly the retained
state above.

## Bounded exclusion: the read path itself cannot latch

Proved in the prior analysis and re-verified: the worker flags are
bracket-released on every exit; `mtk_espim_get_pc`'s error path
(`0x9396a440–0x9396a490`) returns without touching shared mode state;
the chunked reader holds nothing. The only state a *failed read* can
change is (a) skipping `0x9396c0a0` (a missed service, self-healing on
the next successful read) and (b) nothing else — `+29` clears
whenever the helper `0x939645a4` returns; a helper that never returns
is a separate, unresolved question, not a latch in the bracket. Therefore:

> No single failed OEM-12 read leaves a lock or flag held in the SoC
> read path, with the single candidate exception of `+29` (O1).

## Concrete interleavings (model output)

`python3 scripts/emi_interleave_model.py`:

```text
healthy                          oem12 ok/fail 2/0  pkt ok/s5 1/0  misroutes 0  -> none
observed_0509_anomaly            oem12 ok/fail 0/0  pkt ok/s5 1/0  misroutes 1  -> SYNDROME_MISROUTE
raw_wedge_after_crossing_read    oem12 ok/fail 0/2  pkt ok/s5 0/2  misroutes 0  -> SYNDROME_LINK
generic_pending_latch_only       oem12 ok/fail 1/0  pkt ok/s5 0/0  misroutes 1  -> SYNDROME_MISROUTE
obj29_bracket_excluded           oem12 ok/fail 2/0  pkt ok/s5 0/0  misroutes 0  -> none
```

Three conclusions:

1. **The captured `05 09` → `12 00 00` anomaly is reproduced by a
   two-step interleaving** with no fault injection at all: send a
   deferred command (generic-pending set, expected opcode `0x05`),
   let the background `0x12` producer overwrite the retained response
   block before the consumer drains it, then any later trigger runs
   the consumer, which clears generic-pending and copies whatever is
   there — opcode match is never checked (`0x9397940c`–`0x939791c0`).
   This is a **correctness race that exists in healthy firmware**; it
   explains the investigation's anomalous reply without any latch.
2. **A generic-pending latch alone is excluded as the cause of the
   persistent link failure.** With the injection modeling "the request
   that set the flag never receives its response," the scenario
   produces a misroute of a later unrelated response — OEM-12 reads
   still succeed, because the raw read path never consults
   generic-pending. The observed incident unit had *both* OEM-12
   zeros *and* packet status 5; only a failure of the raw window layer
   produces both. This is the bounded exclusion requested: every
   SoC-side flag is either bracket-released, not consulted by the
   failing paths, or — for generic-pending — capable only of misroute.
3. **The persistent-failure syndrome (SYNDROME_LINK) is reachable in
   the model only via a raw-layer wedge.** The `obj29_latch` scenario
   is retained solely to demonstrate its exclusion: complete dataflow
   shows `0x939a1008+29` is bracketed (set `0x93968da8`, helper
   `0x939645a4`, unconditional clear `0x93968de0` on every return,
   including errors), so no firmware path leaves it set. Generic
   pending is likewise excluded (conclusion 2). What the firmware
   cleanup does **not** establish is the physical target's behavior —
   whether the EC-side eSPI/EMI hardware retains an outstanding
   completion after a failed exchange, or accepts another GET_PC
   afterwards. That hardware-credit question, together with the
   raw-layer wedge itself, is the empirical residue; both are
   reboot-cleared on the SoC side and independent of the EC producer
   latch that survives reboots.

## O1: resolved, with an explicitly hardware residue

Complete dataflow closes the firmware half of O1: `0x939a1008+29` is
set at `0x93968da8` and cleared unconditionally at `0x93968de0` after
the submit-and-wait helper `0x939645a4` returns — on success **and**
on error — with `+26`/`+27`/`+28`; every other writer is a bulk
byte-range state reset. The byte means "async request in
submit/await", held for one helper call. No timeout path can leave it
set. The transport replay's modeled credit behavior is therefore
consistent with the firmware on this point. What remains is strictly
empirical and concerns the *physical target*: whether the EC-side
eSPI/EMI hardware retains an outstanding completion after a failed
exchange and whether it accepts a subsequent GET_PC. That cannot be
settled from either image or any model; it needs hardware observation
(for example, distinguishing immediate per-transaction failures from
timeout-paced ones on a unit in the wedged state).

## Assumptions and their falsification

- **B1 (single context):** one execution context per partition. If an
  FF-A endpoint were serviced by a second context, the bracket proof
  still holds (it is per-call, not global), but the misroute race
  window widens; the model's step order would need per-context
  interleaving. Falsify via the FF-A partition manifest in the SoC
  image (execution-context count).
- **B2 (model fidelity):** the model abstracts EC response latency as
  one step (`ec_respond`) and producers as one step (`push12`). It
  cannot express partial block writes; a torn write would add
  misroute varieties, not remove the exclusion in conclusion 2, which
  rests only on "OEM-12 never reads generic-pending" — a static fact.
- **B3:** SCI `0xe2` producer traffic continues during failures only
  if the raw layer is up (producers write via the same window). On
  the incident unit post-failure no new pushes were observed —
  consistent, and predicted by `raw_wedge`.

## What this analysis does not decide

Whether the raw-layer wedge of the incident unit was caused by the
crossing read (the trigger hypothesis), by any read at all, or was
coincidental. The concurrency analysis bounds the *mechanism* (where
persistent state can and cannot live); the trigger remains the
eSPI-spec boundary question recorded in the first analysis, where the
64-byte payload constant and the 4096-byte configured maximum must be
argued separately. Finally, the abstract model here is a companion to
— not a substitute for — the transport replay: agreement between the
two models raises confidence, but only hardware behavior closes O1.

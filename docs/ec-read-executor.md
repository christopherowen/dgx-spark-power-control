# Who executes an EC memory read, and who owns the completion

Static follow-up to the [eSPI read static analysis](espi-read-static-analysis.md),
performed offline against the same pinned images
(`ec_fused.cap` `ab21ddb0…`, `socfw.cap` `0985b848…`; mappings: EC
capsule file `0xd9f` → EC VA `0xc0000`, SoC capsule file `0xa08f0f` →
SoC VA `0x93942000`). All addresses below are from those images unless
stated. Names in quotes are assigned by this analysis from nearby debug
strings; they are not vendor symbols.

## Executive answer

There are **two distinct mechanisms**, and the investigation's phrase
"the EC memory read" covered both:

1. **Raw window reads (OEM command 12, and every register access the
   SoC service makes) are executed by hardware.** The SoC eSPI master
   issues non-posted peripheral-channel memory reads
   (`mtk_espim_put_np_read` / `mtk_espim_get_pc`) against the EC's EMI
   window; the EC's eSPI slave serves them from SRAM. **No EC firmware
   runs for these reads.** The EC dispatch tree `0xc4a88` is not
   involved.
2. **The packet/mailbox protocol (fan service, `05 xx` time queries,
   SCI events, UCSI, RTC commands, power limits) is executed by EC
   software.** The SoC writes a command block into packet SRAM
   (`0x06000800`), rings a doorbell (`0x06000504`), and polls a
   done-status byte (`0x06000802 == 0x80`); the EC's dispatch `0xc4a88`
   (command byte 4–0x14) executes the command and writes the response.
   This protocol *rides on* mechanism 1 for every byte it moves.

"GET_PC" is `mtk_espim_get_pc` — the raw eSPI peripheral-channel fetch
(debug string `0x9397e838`), not a command name. Its error path does
**not** latch the driver object; GET_PC remains callable after a fatal
response at the driver level. The only unresolved SoC-side credit is
the async-in-flight byte at `0x939a1008+29` (set `0x93968da8`; the two
clearers are pinned below and one must be shown reachable on timeout).

## Address-backed trace

### OEM command 12: a SoC-side raw read

The OEM1 endpoint dispatcher (function containing the command switch at
`0x93975fd4`) decodes the command byte as the **last byte** of the
request buffer (`0x93975fb8–0x93975fcc`) and routes case `0x0c`
(`0x93976038`) through `0x93976220` to handler **`0x93975260`**:

- Validates request length (`len+4 ≤ 0x7f`, `0x939752ec–0x939752f4`).
- Extracts two 4-byte fields from the request tail via `memcpy` at
  `0x9397531c` and `0x9397538c` (address and length; see assumptions).
- Requires the length to be `1 … 0x70` (`0x93975390–0x939753a4`) —
  **the service's own bulk-read cap is 112 bytes**, tighter than the
  RESP2 reply cap `0x3fd`.
- Issues **one direct chunked read** at `0x939753d8` → `0x9396c80c`.
- On chunk failure logs
  `"espi_ec_peripheral_mem32_read fail, driver_error = %d"`
  (string `0x93980c68`, xref `0x939753f0`) and returns **status 5** —
  the same value the fan driver names
  `DGX_EC_PACKET_SP_ESPI_READ_FAILED`. Status 5 is the raw-window
  failure code, shared by both mechanisms.

Below the chunked reader, the single-chunk worker `0x93969c60` submits
`{cmd 2, sub 0xa}` through the eSPI request router `0x93961864`; its
error strings are `"PUT NP MEMORY READ 32 - fail"` (`0x9397e7c8`) and
`"mtk_espim_get_pc - fail"` (`0x9397e838`) — raw eSPI transactions.

### The EMI mailbox: EC-software command protocol

The send engine (function at `0x9397905c`) is fully decoded:

1. Read status byte at `0x06000504` (chunked read at `0x939790b4`);
   if busy bits 0–1 set → `"EMI is busy, return and try again later"`
   (`0x939819e0`, xref `0x93979170`), return code `0xa`.
2. Write the command block to packet SRAM `0x06000800` (`0x93979100`,
   via chunked writer `0x9396c6c0`).
3. Write one byte to `0x06000504` as doorbell
   (`0x9397913c`; `"…to fire command fail"` `0x939819a0`).

Completion waits poll response bytes in the same SRAM block:
`0x06000802` must equal `0x80` ("done") or the SoC logs
`"EC process EMI command is not done, something wrong: 0x%x"`
(string `0x939805a0`, xref `0x93973204`), then reads the sub-status at
`0x06000804` and payload at `0x06000805+`. On the EC side, dispatch
`0xc4a88` selects handlers for command byte 4–0x14 (17-entry `tbh`
table at `0xc4a94`) — the opcodes `0x10–0x14` the passive capture
observed, the `0x12` power-measurement branch at `0xc4c30`, and the
deferred `05 xx` worker path through the RTC thread.

### EC controller/window setup

- Four EMI hardware units exist at `0x400F0800/0C00/1000/1400`
  (pointer table at `0xd07a4`, stride `0x400`); per-unit registers
  referenced at `+0x100` (interrupt/command, `0xc6784`) and `+0x108`
  (status, `0xc6770`).
- Window registration, corrected on review: `0xc25b8` configures
  **GPIOs** (its `0xcc7a0` claims and vtable callback belong to the
  GPIO/subsystem setup, not the SRAM map). The SRAM mapping is
  programmed by **`0xc4820` itself**: its middle section writes
  window-base bytes (`0x06000000` little-endian, with a `0x97`
  selector) into the peripheral blocks at `0x400F3900` and
  `0x400F3B00` (literals `0xc4924`/`0xc4928`; stores at
  `0xc48a2`–`0xc48aa` and `0xc48d4`–`0xc4902`). The four EMI hardware
  units at `0x400F0800–0x400F1400` (pointer table `0xd07a4`) remain
  the per-unit register targets.
- Host `0x06000500` is an event/consume register and `0x06000504` the
  status/doorbell byte inside this window. **Mapping corrected on
  review:** the window is piecewise — host `0x06000600` maps to EC
  `0x119000` and host `0x06000800` to EC `0x118c00` (the packet
  block). EC `0x118c00` therefore **is** the packet buffer's EC
  address after all: the RTC thread's deferred-command path reads the
  subcommand at `0x118c01` (`0xc19b8` region, literal `0xc1a14`), and
  fourteen code references anchor `0x119000` as the mirrors/base
  region (literals `0xc1a18`, `0xc3a1c`, `0xc3c20`, among others).
  This section originally *withdrew* the `0x118c00` identification on
  the observation that `0x118c00` also appears as a load-time literal
  in the time-packing code (`0xc17a2`–`0xc17b8`); that observation
  stands, but the withdrawal's conclusion was wrong and is retracted.

## Completion credits and timeouts

- **Worker bracket (raw reads):** the mode flags at
  `0x939a1008+{42,43}` are save/restored by the workers and cleared on
  every exit (proved in the prior analysis). `+43 = 1` is set by every
  asynchronous transaction submit in the `0x939672xx–0x939689xx` chain;
  `+42 = 1` marks synchronous mode.
- **`mtk_espim_get_pc` (`0x9396a1e0`):** on collection failure
  (`0x93969b1c` → nonzero) it logs `0x9397e838` and returns the error
  **without touching `+42/+43/+29`** (`0x9396a440–0x9396a490`). A fatal
  response therefore leaves the driver object consistent; the next
  GET_PC proceeds. This is the direct answer to "does GET_PC remain
  usable after a fatal response": **yes, at the driver-object level**.
- **Async-in-flight byte `0x939a1008+29` — resolved by complete
  dataflow; it is a bracket, not a latch.** The writer at `0x93968da8`
  (set to 1) is inside the async-submit function whose tail calls the
  submit-and-wait helper `0x939645a4` and then **clears the byte
  unconditionally at `0x93968de0`** — on the helper's success *and*
  error returns alike, alongside `+26`/`+27`/`+28`. Its meaning is
  "async request being submitted/awaited" for the duration of one
  helper call. Every other writer found (`0x93957644`, `0x9395799c`,
  `0x93970420`) is part of bulk byte-range state resets (`and` with
  zero over consecutive offsets of the context object), not
  conditional latch management; the reader is the status-decode wait
  `0x93960658` (`[obj+29]` selects the async branch). No firmware path
  can leave `+29` set. **What this does not establish:** firmware
  cleanup says nothing about the *physical target* — whether the
  EC-side eSPI/EMI hardware retains an outstanding completion after a
  failed exchange, or whether it accepts another GET_PC afterwards.
  That hardware-credit question is the sole residue of O1 and is not
  decidable statically.
- **EMI mailbox credit:** the slot is the packet SRAM block plus the
  busy bits in `0x06000504`. The SoC checks busy before sending and
  returns `0xa` (busy) rather than 5 — the incident unit returned 5,
  not `0xa`, so the observed failure is **raw-window transport
  failure, not a stuck mailbox slot**. The EC-side deferred-request
  lifetime (packet SRAM read after mutex release; helper `0xc49b0`
  with `0x18000`-tick ≈ 3 s timeout) is the EC-side race already
  documented, not a credit leak.
- **Retained completions:** EMI mailbox responses persist in packet
  SRAM until overwritten; the consumer `0x939791c0` clears the
  output-available flag only at its start, after the response was
  produced. Raw GET_PC has no retention — each transaction fetches
  fresh. The retained-completion model therefore holds for the mailbox
  layer (as the fan driver's recovery assumes: "reads may drain a late
  completion") and is inapplicable to raw reads.

## Explicit assumptions

- **A1:** the request tail parsed at `0x9397531c`/`0x9397538c` is
  `{address(4), length(4)}` preceding the terminal command byte. The
  lengths validated (≤ `0x70`) and the runtime contract (probes pass
  address/length; 81-byte read accepted) support this; the exact
  FF-A-to-SHM packing that orders the command byte last was not
  traced. Falsify by decoding the RESP2 writer
  (`0x9396c000` region) side by side with a captured SHM request.
- **A2:** "PC"/"NP" in the debug strings mean eSPI peripheral-channel
  and non-posted transactions respectively, per the eSPI
  specification's terminology. Falsify against the spec's command
  encodings in the MMIO writes of `0x93960658`'s family.
- **A3 (retired):** the former assumption about `0xc25b8`'s callback
  arguments is withdrawn — `0xc25b8` configures GPIOs, not the SRAM
  map (see the window-registration correction above).
- **A4:** the EC dispatch `0xc4a88`'s command range 4–0x14 is the
  mailbox protocol, with nothing above 0x14 routed. Falsify by
  decoding the full `tbh` table targets at `0xc4a98`.

## Falsification criteria for the executor split

- If the EC `0xc4a88` tree contains any handler reachable from an
  OEM-12 request (e.g., a re-dispatch of command `0x0c`), the
  "hardware-served" conclusion for bulk reads fails.
- If the raw read path can be shown to require `0x06000802 == 0x80`
  (mailbox done) before returning data, the two mechanisms collapse
  into one and the executor is EC software.
- Runtime (when hardware work resumes): a passive capture during a
  healthy period should see **no packet-SRAM traffic** correlated with
  OEM-12 reads if the split holds — the passive capture at 12:24 on
  the incident unit is consistent (packet opcodes present were the
  periodic `0x10–0x14` pushes, not read echoes).

## Relationship to the fault

On the incident unit after 13:20, **both** mechanisms failed
(OEM-12 returned zeros through the RESP2 mask; packet submits returned
5 immediately). Since both ride the raw-window layer, a single wedge
at the raw layer — EC-side eSPI/EMI hardware or the SoC controller
state — explains the combined failure without any EC-firmware
involvement. The originally affected unit, after a host reboot,
recovered the raw layer (reads return real data) while its EC
producers stayed dead — evidence that the raw-layer wedge and the
producer stop are **two independent latches**, only the first of which
a host reset clears. The producer side is analyzed in
[EC producer dependencies](ec-producer-dependencies.md).

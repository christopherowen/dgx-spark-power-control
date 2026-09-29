# Diagnostic read candidates from the pinned firmware

Offline trace of the pinned images (`ec_fused.cap` `ab21ddb0…`, EC VA
base `0xc0000`; `socfw.cap` `0985b848…`, SoC VA base `0x93942000`)
answering: which host-readable diagnostic fields exist that the
retained probes do not use, and which mailbox commands could become
reads after vetting. Nothing here was executed; candidates marked
*active* require the investigation's full call-path and safety review
before any request is sent, per its standing rules.

## Window-read candidates (OEM-12; not automatically "safe")

These lie inside mapped ranges and fit the retained fixed-address
probe pattern — but "read-only" is not free of effects: every
successful OEM-12 read runs the SoC housekeeping service
(`0x9396c0a0`), which can service pending background sends, and the
RESP2 wrapper masks inner read failures (zeros are not proof of
zeros). A mapped address alone does not establish a safe new probe;
each candidate still needs the investigation's review before use.

| Host address | Evidence | Value |
| --- | --- | --- |
| `0x802/1`, `0x804/1` | response-block status read by the SoC completion wait (`0x939731a4`; `0x802 == 0x80` = done) | whether the EC considered the last mailbox command complete — retroactively interpretable in every past passive capture's packet bytes too |
| `0x900/1`, `0x904/1` | read by the firmware's own debug dumper (`0x93977f9c`+) | vendor-designated diagnostic state bytes; meaning pending EC-side writer trace |
| `0x632/2`, `0x636/2`, `0x65f/2` | read by handler `0x93972ecc` before each exchange | protocol/state fields; meaning pending |
| ~~`0x776–0x77d`~~ | **withdrawn on review:** the base at `0xc4acc` (literal `0xc4ccc`) is `0x11a8f9`, not `0x119000` — the cached-time fields live at `0x11aa6a–0x11aa70`, **outside** the host mirrors. Reading host `0x776–0x77d` would not observe that clock; the proposed discriminator is removed until another mapping is demonstrated |
| ~~`0x78e/1`~~ | **withdrawn on review:** the value stored at `0xc18ba` comes from `0xcc3e6(0x0e)` — an RTC-register read (I2C bus 0, addr `0x32`) taken at startup. It is a sample, not a thread identity or heartbeat; its presence cannot establish continuing RTC-thread progress |
| `0x790–0x793/4` | fan-floor mirror, written `FF` at init (`0xc3ae4`–`0xc3af0`) | renames the probe's "thermal_mirror" — actually fan floor |

Named during the same trace (for the record): time mirror
`0x788–0x78d` (writer region `0xc14f0`), budgets `0x714–0x72c`
(publisher region `0xc3412`). A full field map of the window needs
tracing all 14 code sites that use the `0x119000` base pointer (EC
code addresses fields via base+offset, so literal scans cannot
enumerate them); the subset above is what one pass yielded.

## Mailbox commands (active — vetting required)

Decoding the `tbh` table at `0xc4a98` (17 entries, target =
`0xc4a98 + 2·halfword`) resolves the EC dispatch `0xc4a88` completely:

All 17 entries individually (entry word at `0xc4a98 + 2·(cmd−4)`;
target = `0xc4a98 + 2·entry`):

| Command | Entry | Target | Reading |
| --- | --- | --- | --- |
| `0x04` | `0x0011` | `0xc4aba` (shared epilogue; body above) | undecoded — the one remaining small handler to classify |
| `0x05` | `0x0016` | `0xc4ac4` | time-query family (`05 01` cached branch reads the `0x11a8f9+0x176` fields); response-ownership caveats |
| `0x06` | `0x0116` | `0xc4cc4` = `pop {r4,r5,r6,pc}` | unsupported (returns) |
| `0x07` | `0x0039` | `0xc4b0a`, calling `0xc3900` | **implemented** — the fan getter/setter family (correction: the earlier "0x06–0x0e unsupported" claim was wrong for `0x07`) |
| `0x08`–`0x0e` | `0x0116` | `0xc4cc4` | unsupported |
| `0x0f` | `0x0045` | `0xc4b22` directly | sub-dispatch family member |
| `0x10`–`0x14` | `0x006e` | `0xc4b74` shared entry, sub-dispatch to `0xc4b84`/`0xc4bfa`/`0xc4c30` | measurement/report family (locks per the startup cross-check); stateful — not read candidates |

Entry and target values verified directly against the capsule's 17
halfwords at `0xc4a98` (target = `0xc4a98 + 2·entry`; e.g. `0x006e·2 +
0xc4a98 = 0xc4b74`). The hypothesis of a broad family of hidden
read-only getters stays closed — on the corrected evidence: only
`0x04` remains unclassified, and `0x07` is the known fan interface.

## SoC partition log ring

The 8 KiB ring exists (buffer via `0x9398a018`, index `0x9398a010`,
char sink `0x93945e08`). **Correction on review:** the previously
cited "reader API" was misidentified — the `0x93943xxx` cluster is the
*configuration store*, and `0x93945ba8` belongs to random-number
generation; neither establishes a ring export, and no export path is
currently identified. The ring's *contents* are also not claimable:
without knowing the runtime log level and whether the ring has
wrapped, it cannot be described as holding the incident's WARN
history. The corrected evidence and the actual console conduit
analysis are in
[firmware-log-visibility.md](firmware-log-visibility.md); tracing the
real `FFA_CONSOLE_LOG` route into the monitor is the assigned next
step on this seam.

## Trace-support audit (firmware evidence plus one read-only OS check)

- **SoC partition instrumentation: present, with an emission path —
  corrected on review.** The ring is *not* "always on": emission is
  gated by the runtime log level (`0x93945c44` threshold). And a zero
  backend pointer at `0x9398c018` does **not** mean "no emitter": the
  binary treats NULL as selecting the built-in conduit at
  `0x93946b64`, leading to **FFA_CONSOLE_LOG**. An emission path
  therefore exists; where the monitor sends that output remains open,
  and tracing the FFA_CONSOLE_LOG route is preserved as a live
  investigation (see [firmware-log-visibility.md](firmware-log-visibility.md)).
- **EC instrumentation: present, runtime-gated in private RAM.**
  Tracepoint facility `0xc30c4` (the `trace 0xe0–0xe2` calls) checks
  a gate byte before formatting and emitting; the gate is not host-
  reachable through any traced command.
- **Silicon trace units: no firmware support found on either side.**
  (The log ring's *contents* are likewise not claimable as incident
  history — see the log-visibility correction above.)
  The EC image contains zero references to DWT/TPIU/debug registers
  (two ITM-range constants appear, likely data); nothing programs the
  M4 trace units. On the AP side, a read-only check of this kernel
  enumerates **no ETM/ETB/ETF/TPIU devices** (device tree has none),
  though `arm_spe_0` (Statistical Profiling Extension) is registered.
  SPE samples non-secure execution only — it cannot observe the
  secure partition, and neither facility touches the EC chip.
- **eSPI controller "trace":** the SoC driver's debug output is
  print-only (IRQ/DMA status strings, a test-data dumper); no hardware
  trace FIFO or drain register was found in either image.
- **External bus analyzer:** not settleable from firmware — an
  eSPI-lane probe is a board-level question. "The hardware lacks
  tracing support" is therefore **not established**; what is
  established is that no firmware-mediated trace path is currently
  reachable from the host.

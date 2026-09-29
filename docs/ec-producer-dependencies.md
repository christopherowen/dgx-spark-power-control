# EC producer dependencies: what can stop budgets, time, and queries together

Static follow-up to the [eSPI read static analysis](espi-read-static-analysis.md)
and [Who executes an EC memory read](ec-read-executor.md). Pinned
images as before (`ec_fused.cap` `ab21ddb0…`, EC VA base `0xc0000`).
Objective: a dependency graph of the EC's publisher tasks detailed
enough to explain the originally affected unit's combined symptoms —
**zero package and system budgets, frozen RTC publication, alive
command handler (`05 01` answered), dead `05 09` worker reply,
continuing background `0x10–0x14` packet traffic** — and to identify
host-accessible observations that discriminate the candidate cut
points. The window-reinitialization result (`0xc4820`) explains the
cleared mirrors and the one published timestamp; this analysis asks
what must additionally be true for the periodic producers never to run
afterwards.

## Verified dependency graph

Nodes are EC functions/objects; labels carry the pinned addresses.
"Gates" are conditions a consumer requires to make progress.

```text
                         power-on sequencing (0xc1b08)
              polled-PLTRST branch ──┐        ┌── event-0x40 branch
              posts to 0x11a044      │        │  returns WITHOUT posting
                                      ▼        ▼
                        shared event object 0x11a044
                         │                        │
        ┌────────────────┘                        └───────────────┐
        ▼ initial wait (mask 0x1)                  ▼ SAME wait — taken ONCE
  RTC thread 0xc188c                               query worker 0xc38b0
  ├── writes RTC register (0xcc41a: reg 24←12,      thereafter delay-paced:
  │     I2C bus 0, addr 0x32)                       loop: power state =
  ├── loop on private event 0x118b04 (mask 0x7):      0xc1bf0() == 2 AND
  │     bit1 → re-read RTC + publish time             0x11aa93 == 1 →
  │     bit2 ← 1 Hz timer callback                    trace 0xe2/0xe1 pushes;
  │     bit0 → deferred EMI command                   no further event wait
  │          0xc49b0 (mutex, 0x18000 ticks ≈ 3 s)
  │          then subcommand from packet block 0x118c00
  │
  │   1 Hz timer: object 0x119368, installed and started by the
  │   stage-4 init record 0xcfc40 (walker 0xca7ec/0xca810, run at
  │   0xca834 BEFORE the static-thread loop 0xca83a), period 0x8000
  │   ticks = 1 s @ 32.768 kHz. On expiry the kernel queues the next
  │   period at 0xcbd4e BEFORE invoking the callback at 0xcbd68;
  │   callback 0xc3cd0 posts mask 0x2 of 0x118b04, then submits work
  │   object 0x1184d0 via 0xcaf98 (work submission, not re-arming;
  │   its handler 0xcc93f returns immediately)

  host-boot window reinit 0xc4820 (PLTRST work 0xc2760 path):
     clears budget/time windows (512 B + 1024 B)
     preserves 16 table-indexed metadata bytes
     programs the SRAM map: base 0x06000000 (+ selector 0x97) into
        peripherals 0x400F3900 and 0x400F3B00 (0xc48a2–0xc4902)
        (host 0x06000600 → EC 0x119000, host 0x06000800 → 0x118c00)
     reads version fields once (0xc7ce4–0xc7d08 → +0x160–0x163)
     tail: 0xc373c → clears completion latches 0x11aa97/98/99
           0xcc6d6 → 0xc14d4 publishes RTC time once
           0xc3758 → sets publication enable 0x11aa93 = 1
     (0xc25b8 elsewhere configures GPIOs — not this map)

  thermal thread 0xc3a28, per iteration (re-checked every loop):
     power state 0xc1bf0() == 2, else skip
     sensor init: if [0x11aa99]==0 → 0xc3764() (ten bus-2 sensor
        writes), then [0x11aa99]=1 — 0xc4820's tail CLEARS this flag
        (0xc373c), so the iteration after every host-boot window
        reinit retries all ten writes (verified by replay)
     fan floor: if [0x11aa97]==0 → write four FF bytes, set 0x11aa97
        (0xc3ae4–0xc3af6); nonzero skips only that init, not later work
     package path: if [0x11aa98]==0 && [0x11aa93]!=0 → 0xc3d4c()
        publishes constants from lookup 0xc5d90 (140000/142000 mW —
        NOT sensor-derived, so returned sensor errors still publish);
        on success [0x11aa98]=1, delay 1 s
     periodic path under locks (below): snapshot, plausibility,
        publish via 0xc3408
     package publisher 0xc3d4c; system publisher 0xc3e24 from
        PLTRST work 0xc2760; runtime path 0xc3394→0xc3d84
```

## What each observed symptom pins down

| Symptom (affected unit) | Consequence in the graph |
| --- | --- |
| `05 01` answered with the frozen timestamp | EMI mailbox dispatch (SoC send engine → EC `0xc4a88`) alive; the cached-time branch reads the mirror directly — no thread needed |
| `05 09` reply was `12 00 00` (wrong) | consistent with the RTC thread not processing the request **or** with it processing the request and the retained block being overwritten before the SoC consumed it (the misroute race). A wrong reply alone does **not** prove the RTC thread never ran |
| background `0x10–0x14` traffic continues | the SoC-side send machinery is alive; the traffic's producers include SoC-initiated pushes — at least opcode `0x12` is built by the SCI-`0xe2` handler `0x93978440` and submitted via the common sender `0x93979030`, and the read-side housekeeping `0x9396c0a0` (run after every *successful* chunked read, `0x9396ca2c`) services pending sends. Packet activity therefore does **not** by itself prove the EC's query workers ran |
| budgets all zero | the *observation* is zero at the mirror. Candidate explanations, none established: publication paths never completed after the window clear (suspension or permanent block included but not required), a later window clear overwriting published values, or read fidelity — OEM-12's masked inner failures mean observed zeros may be failed reads, not zero data. Not explained by sensor errors (constants publish regardless) |
| time frozen at the boot timestamp | the RTC thread's periodic publish stopped; the last write is `0xc4820`'s one-shot `0xcc6d6 → 0xc14d4` publish |

The load-bearing result survives the corrections with sharper
wording: the query workers' gates (power state 2, `0x11aa93`) are
re-checked every loop, and `0x11aa93 = 1` is required — the observed
*SoC-side* traffic does not certify those gates, but nothing observed
contradicts power state 2 either. With the sensor-retry and timer
ordering corrected (see the
[startup cross-check](ec-startup-cross-check.md)), the cheap
explanations are gone: sensor init **is** retried after every window
reinit, sensor errors still publish the 140/142 W constants, and the
timer is installed unconditionally with the kernel queuing the next
expiry before the callback. What remains to explain zero budgets and
frozen time is a state that *survives warm reboots and retries* —
which points at suspension (an operation that never returns) rather
than any missed retry or cleared flag.

## Blocking and ownership: the locks

Acquire is `0xcc890(lock, -1, -1)` — **infinite timeout**; release is
`0xcc8b0`. Corrected against the
[startup cross-check](ec-startup-cross-check.md) — the earlier table
mistook literal-pool entries (`0xc4cd0`, `0xc4cd8`) for acquisitions:

| Lock | Real acquisition sites | Notes |
| --- | --- | --- |
| `0x11a018` (A) | thermal thread `0xc3b06`; released `0xc3b64` | outermost in the thermal periodic path; **not** taken by `0xc4820`'s preserve loop, by `0xc5da0` (ROM pointer) or `0xc5da8` (fixed config writes) |
| `0x11a004` (B) | `0xc37bc` (first take/release in the thermal loop); command `0x10` handler `0xc4b90`, unlock `0xc4bd6`; thermal path takes/releases it under A | literal `0xc3858` belongs to `0xc37bc`, not the query worker |
| `0x119ff0` (C) | command `0x11` handler `0xc4c04` and command `0x12` handler `0xc4c3a`, both branching to unlock `0xc4bd6`; thermal path takes/releases it under A | no B→C nesting exists anywhere found |

The command handlers (`0x10`/`0x11`/`0x12`) update data, unlock, and
only then send the reply through `0xc4a4c` — no packet-completion wait
was found inside any critical section. The RTC helper `0xc49b0` uses a
*separate* mailbox mutex (`0x118a28`): its acquisition is infinite;
the `0x18000`-tick (~3 s) bound applies only to output-full polling
**after** acquisition, and therefore cannot serve as a release
guarantee for A, B, or C.

## What remains reachable, and what is excluded

Excluded by the cross-check and its replays (see
[ec-startup-cross-check.md](ec-startup-cross-check.md),
[producer replay](../diagnostics/ec-publication/replay_ec_producers.py)
and [timer replay](../diagnostics/ec-publication/replay_ec_timer.py)):

- *Missed sensor retry* — the sensor-init flag is `0x11aa99` (literal
  `0xc3c00`, read at `0xc3aa4`, set at `0xc3ab0`) and window
  initialization clears it; the next thermal iteration retries all ten
  bus-2 writes. The earlier `0x11a8f9` attribution was a literal-slot
  error (`0x11a8f9` is a structure base).
- *Sensor errors → zero budgets* — the package lookup `0xc5d90`
  supplies constants; `0xc3d4c` publishes 140000/142000 mW regardless
  of returned sensor errors. Zero budgets are not explained by failed
  sensors.
- *Timer never armed* — the initializer record runs unconditionally
  before static-thread creation, and the kernel queues the next expiry
  before invoking the callback; a failed work submission (`0xcaf98`,
  object `0x1184d0`) leaves both the requeue and the RTC event intact.
- *B→C lock parking with a 3-second self-heal* — the `0x12` path
  acquires only C and releases before replying; no B→C nesting exists,
  and `0xc49b0`'s timeout bounds a different mutex's post-acquisition
  poll.

What remains possible — as *a* candidate, not the only one — is **an
operation that never returns** (an unbounded bus-2 or bus-0 I2C
transaction, or a suspension inside one of the *actual* critical
sections: A/B/C or the `0x118a28` mailbox mutex). It is *not*
established as the explanation: unpassed gates (power state,
publication enable), an event-blocked consumer (a thread still parked
on an initial wait), a later window clear overwriting published
values, and read fidelity (OEM-12's masked inner failures —
"observed zeros" may be failed reads, not zero data) all remain
unresolved alternatives. Note also the eligibility caveat: clearing
the initialization flags makes retries *eligible*; it does not prove
the thread executed them, since no observation confirms a post-clear
thermal iteration actually ran. Both candidate forms are
consistent with all observations: retries happen and fail to restore
publication (the suspended thread never reaches them), the state
survives warm reboots (the EC context is never reset), and unrelated
paths continue. No release mechanism is proven for this class — in
particular, "EC reset is the only possible recovery" is **not
established**; identifying a release requires either live task/owner
state or a concrete suspension path through the real critical
sections, neither of which exists yet.

## Host-accessible discriminators and their side effects

| Observation | How | Discriminates | Side effects |
| --- | --- | --- | --- |
| Mirror snapshot (budgets, time, status `0x06000504`, packet block `0x06000800/8`) | retained passive probe (OEM-12, fixed pairs) | confirms symptom set; packet opcode stream shows the *SoC-side* send machinery alive (see caveat above) | not effect-free: every successful read runs the SoC housekeeping service (`0x9396c0a0`), which can service pending background sends; no event-register reads |
| `05 01` cached-time getter | FF-a OEM command | command path alive (already established) | none observed; served from mirror |
| `05 09` worker status | **not safe** — deferred-ownership race documented; and per above, its reply cannot authenticate RTC-thread processing either way | — | can consume/misroute a retained completion; excluded by the investigation |
| Gate bytes via a wider window capture | **not available**: the gate/lock/timer state (`0x11aa93–0x11aa99`, locks `0x119ff0/0x11a004/0x11a018`, timer `0x119368`) lies **outside** the host-mirrored ranges (EC `0x118c00+` and `0x119000–0x1191ff`), provable by address alone. No host read can see them; a wider capture adds nothing | — | — |

Existing host operations and progress restoration, traced:

- **Warm reboot** re-runs `0xc4820`, which clears the publish-once
  latches and forces a publish retry (`0xc3d4c`) — already happening
  on the affected unit, and insufficient, which is itself evidence
  that the failure is *not* a merely-missed publish. It does not
  reset the EC context (a suspended thread or held lock survives it)
  — which is why reboot-visible retries still fail to restore
  publication if the mechanism is suspension.
- **Host RTC commands** (the `0xcc41a`-family writers; RTC_CMD_SRT et
  al.) exercise the RTC's I2C-bus-0 path and change the time —
  side effect: overwrites the frozen-timestamp evidence; touches
  neither the timer, the sensor-init flag, nor any lock.
- **Host subcommands `0x0f/0x12–0x13`** drive the power-button thread
  and **`0x0f/0x1c–0x1d`, `0x1f–0x21`** schedule EEPROM-flag work —
  none reach the producer mechanisms traced here.
- **EC software reset** requires host power-down (outside the
  no-restart objective, per the investigation).

No traced host operation is *proven* to restore producer progress —
and no path is proven **unable** to: with the cheap sequences
excluded, whether any operation releases a suspended owner is exactly
the open question. The mirror snapshot remains the only safe probe,
and it cannot reach the decisive state.

## Open items

- **O2:** decode `0xcf994`/`0xcf990` (event wait/clear semantics) to
  fix what the RTC thread's initial wait and the query workers'
  one-time wait consume, and whether the `0xc1b08` event-`0x40`
  branch's missing post can starve either.
- **O3 (new, central):** find a concrete suspension path — an I2C or
  mailbox operation that can fail to return — inside one of the real
  critical sections (A/B/C, or the `0x118a28` mailbox mutex whose
  acquisition is infinite), or obtain live task/owner state that
  shows a holder parked. This is the *preferred* route to a mechanism
  and a release story — not the only one: unpassed gates, an
  event-blocked consumer, later window clearing, and read fidelity
  remain unresolved alternatives per the qualification above.
- **O4:** decode `0xc1b08`'s two branches and `0xc2760`'s call of
  `0xc3e24` to order the system publisher against the flag clear and
  the locks.

Resolved on review: `0xcc6d6` in `0xc4820`'s tail is the one-shot RTC
publication path calling the reader `0xc14d4`; `0xcc41a` writes an
RTC register (I2C bus 0, addr `0x32`), it is not a timer
registration; `0xc25b8` configures GPIOs and `0xc4820` programs the
SRAM map; the query workers take their boot-ready wait once and are
delay-paced thereafter; the thermal loop re-checks its initialization
state every iteration. A second review round (commit `58fd0c4` and
[ec-startup-cross-check.md](ec-startup-cross-check.md)) further
resolved: the sensor-init flag is `0x11aa99` and **is** cleared by
window initialization (retries happen); sensor errors do not prevent
publication of the 140/142 W constants; the timer initializer runs
before static-thread creation and the kernel requeues each expiry
before the callback; `0xc4cd0`/`0xc4cd8` were literal-pool entries,
not lock acquisitions — command `0x12` acquires only `0x119ff0` and
releases before replying.

## Assumptions and falsification criteria

- **C1:** `0x11a044` posts are broadcast (consumers do not consume;
  no clear call is present in `0xc188c`'s initial wait or `0xc38b0`'s
  one-time wait). Falsified if `0xcf994` auto-clears — then a single
  consumer starves the others and the boot-ready analysis of O2
  changes shape.
- **C2:** the producers and the query workers check the same shared
  gates (power state 2 at `0xc3aa0`/`0xc38d0`; publication enable
  `0x11aa93` at `0xc3abc`/`0xc38d4`). Falsified if further decoding
  shows either path checking a different condition.
- **C3:** `0xc4820` runs and completes its tail on every host boot
  (the frozen timestamp advancing to each boot's time is direct
  evidence the one-shot publish executed). What it does **not** do —
  reset the EC context or its suspended threads — is what lets a
  suspension-class failure survive reboots even though every retry
  path it clears does re-run.

## Relation to the read-fault investigation

Nothing in this graph is touched by an OEM-12 read: the producers are
gated by EC-internal events, timers, flags and I2C buses, none of
which the raw window read path consults. The read-path fault and the
producer stop are therefore mechanistically independent latches — the
first cleared by a host reboot (link/partition reset), the second
surviving it — which is exactly the split observed between the
incident unit and the originally affected unit after its reboot.

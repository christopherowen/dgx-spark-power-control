# EC startup, publication retry, and lock ownership cross-check

This review checks proposed sensor-init, timer-init and lock-parking causes
against the same EC 3.5.8 capsule used by the existing replays: SHA-256
`ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82`,
file offset `0xd9f` mapped to EC VA `0xc0000`. The capsule, disassembly and
saved machine data remain outside Git. This is static/offline work; no new
EC request or recovery operation was performed.

The review does **not** identify a live stalled task or establish recovery.
It excludes several specific proposed explanations and identifies the
remaining observation gap.

## Sensor initialization is retried after window initialization

The sensor-init flag is `0x11aa99`, not `0x11a8f9`:

- `0xc3a3a` loads the flag address into `r8` from literal `0xc3c00`.
- `0xc3aa4` reads through `r8`. When clear, `0xc3aaa` calls the sensor
  initializer `0xc3764`; `0xc3ab0` sets the same byte after it returns.
- `0xc373c` clears the fan, sensor and package flags at `0x11aa97`,
  `0x11aa99` and `0x11aa98`. Window initializer `0xc4820` calls that
  function at `0xc4906`.
- The loop returns to `0xc3a9c` at `0xc3b6e`, so these flags are checked
  on subsequent iterations.

`0x11a8f9` is a base used to address a larger data structure. It is not
the sensor-init byte used at the cited instructions. The claim that this
sensor flag survives `0xc4820` is contradicted by the effective addresses.

The expanded [producer replay](../diagnostics/ec-publication/replay_ec_producers.py)
executes a first thermal pass, window initialization, and a supplied next
thermal iteration with its saved registers/stack. Both successful and
returned-error sensor transactions are tested. Each case makes **ten fresh
bus-2 sensor writes** after reinitialization and publishes **140000/142000 mW**
package limits and four `FF` fan-floor bytes. Later policy work, scheduling
and physical I2C remain outside the replay.

Package lookup `0xc5d90` supplies constants; it does not consult sensor
readings. `0xc3d4c` stores them before notification. Thus returned sensor
errors or a failed notification do not imply zero package budgets. An I2C
operation that never returns remains a different, unexcluded possibility.

The fan flag also has a visible setter: `0xc3ae4–0xc3af0` writes the four
`FF` bytes, then `0xc3af6` sets `0x11aa97`. Its nonzero value skips that
initialization; it does not gate entry to all subsequent thermal work.

## Literal references are not lock acquisitions

The command dispatcher has executable acquisition sites and separate
literal-pool entries containing lock addresses:

| Command | Selected executable path | Resource and release |
| --- | --- | --- |
| `0x10` | `0xc4b84`, acquire call `0xc4b90` | B=`0x11a004`, unlock `0xc4bd6` |
| `0x11` | `0xc4bfa`, acquire call `0xc4c04` | C=`0x119ff0`, branch to unlock `0xc4bd6` |
| `0x12` | `0xc4c30`, acquire call `0xc4c3a` | C=`0x119ff0`, branch to unlock `0xc4bd6` |

`0xc4cd0` and `0xc4cd8` are the literal addresses holding B and C. The
`0x12` path does not acquire B and then C. The reviewed paths update data
and unlock before sending the reply through `0xc4a4c`; no packet-completion
wait was found inside those critical sections. Suspending a holder is still
possible in principle, but a reachable suspension/parking mechanism must be
traced separately. A retained-response race alone does not establish it.

In the thermal loop, `0xc37bc` first takes/releases B. Later `0xc3b06`
acquires A=`0x11a018`; the path may take/release B, then take/release C,
before releasing A at `0xc3b64`. These operations occur after the first
package and fan-floor stores. Literal `0xc3858` belongs to `0xc37bc`, not
to the following query-worker function.

The `0xc5da0` helper returns a ROM pointer; `0xc5da8` writes fixed
configuration outputs. Neither acquires these locks. The preserve loop in
`0xc4820` directly copies table-indexed bytes; the claimed A/B acquisition
there is absent. This invalidates the proposed discriminator that a parked
A/B holder must block that preserve loop. Other calls in initialization
have their own dependencies, including the RTC I2C transaction.

Finally, the approximately three-second timeout in RTC helper `0xc49b0`
bounds output-full polling **after** acquisition of the distinct mailbox
mutex `0x118a28`. Its lock acquisition is infinite. It cannot be used as a
three-second release guarantee for A, B or C. See the
[mailbox-wait analysis](no-restart-recovery.md#mailbox-mutex-and-read-based-recovery).

## Timer setup and periodic scheduling

The timer setup entry at `0xcfc40` contains function `0xc3ce9` and a null
device pointer. It is an initialization record, not an independent task
record. The initializer walker `0xca7ec` visits eight-byte entries; for a
null device field it directly calls the stored function at `0xca810`.

Startup calls the walker with stage index 4 at `0xca834–0xca836`. The
boundary table at `0xd0f7c` gives that stage `[0xcfc28, 0xcfc58)`, which
includes the timer setup. The static-thread setup loop follows at
`0xca83a`, with creation calls at `0xca892`. There is no per-entry runtime
condition that skips the timer initializer on this normal startup path.
Earlier startup failure or later timer-state corruption remains possible;
the live init history has not been captured.

`0xc3ce8` installs callback `0xc3cd1` in timer `0x119368` and starts it
with initial and periodic durations of `0x8000` ticks. Original timer-start
code `0xcbdb0` stores the period at timer `+0x28` and schedules the initial
timeout. On a supplied expiry, `0xcbcd0` queues the next period at
`0xcbd4e` **before** invoking the callback at `0xcbd68`.

The callback posts **event mask `0x2`** to `0x118b04`, then submits work
object `0x1184d0` through `0xcaf98`. That is work submission, not periodic
timer re-arming. The initialized work handler is `0xcc93f`; its body at
`0xcc93e` returns immediately.

The [timer replay](../diagnostics/ec-publication/replay_ec_timer.py) passes
three cases: initialization before static-thread setup, two periodic
expiries with successful work submission, and two expiries with supplied
work-submission error `-16`. Both expiry cases preserve the ordering:

```text
queue next expiry -> post RTC event mask 0x2 -> submit separate work
```

The replay executes the reviewed initializer, timer start, expiry and RTC
callback instructions. Other startup hooks, timeout-queue operations,
the tick value, event delivery and work submission are modeled boundaries.
It does not run a scheduler or demonstrate actual timer progress. It does
show that failure of this separate work submission cannot prevent the
already-performed periodic requeue or event post.

## SoC model and recovery conclusions

The SoC `+29` correction is supported: `0x93968da8` sets the byte and
`0x93968de0` clears it after helper `0x939645a4` returns, including returned
errors. State while a helper has not returned and physical completion-credit
behavior are separate questions. This does not supply a host recovery call.

The reviewed abstract interleaving model still does not use its `gp_latch`
injection. Running its generic-pending scenario with or without that flag
produces identical state; it is not an executed persistent-latch test. The
new `obj29_bracket_excluded` step assigns true then false by construction.
Its exclusion rests on the binary control-flow proof, not independent model
evidence. The accompanying recorded output also contains the superseded
`obj29_latch_only` scenarios and needs regeneration.

The corrected SRAM ranges exclude the identified internal gate, mutex-owner
and timer objects. Reading more of those same windows cannot expose them.
That limitation is narrower than a proof that no possible debug or host
interface can observe the state.

The proposed combination of an uncleared sensor-init latch and a timer
whose callback fails to re-arm does **not** reproduce the observed syndrome:
the former latch identification and the latter re-arm dependency are wrong.
No traced, verified host operation currently restores the producers, but
that is not a proof that EC reset is the only possible recovery. Nor has
reset been demonstrated to repair the current fault in this follow-up.

The remaining work is to identify a verified way to observe the live EC
task/owner state, and to trace concrete suspension sites against the actual
critical sections. For RTC, distinguish a pending periodic event with a
blocked consumer from a timer/interrupt fault. For thermal publication,
distinguish a pre-publication I2C wait, task suspension, failed power/enable
gates, and a later window clear. None is established as the live cause.

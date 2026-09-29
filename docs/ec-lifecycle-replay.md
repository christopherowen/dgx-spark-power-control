# EC publication lifecycle replay

The fixed offline replay in
[`replay_ec_lifecycle.py`](../diagnostics/ec-publication/replay_ec_lifecycle.py)
executes selected Thumb instructions from the EC 3.5.8 release capsule. It
demonstrates a path that erases system publication without republishing it,
and narrows several competing explanations. **It neither establishes dgx3's
cause nor provides a live recovery operation.**

The capsule SHA-256 is
`ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82`.
Code is mapped from file offset `0xd9f` to `0xc0000`. Initialized data is
copied from `0xd1854` to `0x118000` with exclusive end `0x118b10`, matching
startup routine `0xcba1c`. The replay copies precisely `0xb10` bytes; it does
not execute firmware boot.

## Window reinitialization can erase published system limits

Power-state monitor `0xc1a60` calls the shared-window initializer `0xc4820`
when the observed state **changes to 2**, the requested-state byte is 2, and
the initialized flag at `0x11a8f0` is clear. Leaving observed state 2 clears
that flag. An unchanged state 2 returns without retrying initialization,
even if the flag is clear. The replay also executes the state decoder
`0xc2538`: logical GPIO combinations `000/001/011/101/111` yield states
`5/3/2/6/2`; the other combinations return `-14`. These are logical inputs,
not verified board signal names or sampled levels from dgx3.

The initializer zeroes the shared `0x200`-byte window and `0x400`-byte packet,
restores 16 selected metadata bytes, refreshes status/version fields, clears
three thermal initialization flags, reads RTC once and enables publication.
Budgets and RTC time are absent from the preserved-byte table. Status byte
`0x19c` is initially restored but subsequently rewritten by `0xc4288`.
The initialization path includes RTC register access; it is not merely a
memory operation and is not suitable for blindly calling on a running EC.

A sequential replay provides this counterexample:

| Supplied execution order | System mirror (W) | Internal system limits (W) |
| --- | --- | --- |
| PLTRST work `0xc2760` completes publisher `0xc3e24` | 231 / 244 / 257 / 265 | 231 / 244 / 257 / 265 |
| Monitor observes state 2 → 3 → 2 with requested state 2 | 0 / 0 / 0 / 0 | 231 / 244 / 257 / 265 |

The monitor does not invoke the system publisher or post boot-ready. A later
PLTRST work invocation can republish, but this replay supplies no such event.
The ordering is an explicit input, **not a reproduced hardware transition or
proof of a race**. Thermal flags are cleared to permit package initialization
again; this result alone does not explain persistent zero package budgets or
the frozen time mirror.

## Explanations the replay constrains

| Candidate | Original-instruction result | Limit of inference |
| --- | --- | --- |
| Missing boot-ready alone prevents system publication | PLTRST work publishes even when its boot-ready wait returns zero after the supplied timeout. | A blocked work item or a later window clear remains possible. |
| Every successful power-on wait posts boot-ready | The event-`0x40` branch returns without posting it; the polled-PLTRST branch posts it. | The replay supplies event timing; it is not an RTOS schedule. |
| Ordinary returned RTC I2C error preserves old time | Reader `0xc14d4` replaces all six mirror bytes with `FF` on `-5`; supplied successful fresh data advances the mirror. | An unreturned call, absent invocation, stale device data or host read-fidelity problem is not excluded. |
| A missed group resume stops RTC and thermal tasks together | The suspend/resume table excludes RTC. It includes SMC, THRMLMGMT and both temperature/system-power query tasks. | Recorded query activity constrains a group-wide explanation; it does not reveal any individual task's current state. |

The group helpers `0xc7d38/0xc7d58` select the same four entries from the
nine-thread table at `0xd07d4`. Start helper `0xc7d1c` selects all nine.
The replay records calls at the scheduler boundary; it does not run those
tasks or claim that a resume necessarily makes a blocked task runnable.

## Scope and remaining evidence

All 14 scenarios and the eight-input state-decoder check run in emulated
memory. GPIO, I2C, event waits, work scheduling and thread operations have
explicit simulated boundaries. Board revision is supplied as zero; the
PLTRST work configuration getter is supplied as zero to exercise its wait.
The system constants, publication stores, clearing/preservation loops,
relevant state callbacks and branch decisions execute original instructions.
Execution outside the reviewed ranges or the instruction budget fails.

This is a release-image interpretation, not a live flash attestation. It has
no Linux module, arbitrary-call CLI, network access or physical MMIO access.
The proprietary capsule and generated traces remain outside Git.

The next discriminating evidence is the live EC's producer task/work state
and whether the window was reinitialized after its system publisher. That
requires a verified firmware debug route or vendor instrumentation. The
normal OEM RESP2 dispatcher `0x93975edc` was also reviewed across commands
1–18: it routes flash/RPMC, short I/O, memory, OOB, GPIO, generic-mailbox and
error-history operations. No standalone GET_PC drain route was identified.
The two identified direct GET_PC callers remain the deferred mem32/mem64
read paths. This bounded audit does not prove that no indirect or separate
debug interface exists, and does not justify sending a malformed request to
force one of those branches.

#!/usr/bin/env python3
"""Offline interleaving model for the DGX Spark SoC↔EC read and mailbox paths.

Research artifact for docs/soc-concurrency-analysis.md. Not part of the
driver build or tests; no hardware access. Models the state machines
recovered from the pinned SoC firmware image (socfw.cap 0985b848…) as
abstract transitions and enumerates interleavings under configurable
fault injections, checking which reach the observed failure syndromes:

  SYNDROME_LINK  — every packet submit returns status 5 AND every
                   OEM-12 read fails (RESP2-masked zeros);
  SYNDROME_MISROUTE — a response from producer A is delivered to a
                   consumer that expected producer B's opcode.

Fault injections:
  raw_wedge      — raw eSPI window transactions fail permanently
                   (EC-side or controller-side hardware wedge);
  gp_latch       — the request that set generic-pending never
                   receives its response (retained block untouched),
                   so the flag stays set until any later consumer;
  obj29_latch    — RESERVED/EXCLUDED: the async-in-flight byte
                   (0x939a1008+29) is bracketed in firmware
                   (set 0x93968da8, helper 0x939645a4, unconditional
                   clear 0x93968de0 on every return). Kept only to
                   demonstrate the exclusion. What firmware cleanup
                   does NOT establish is whether the physical target
                   retains a completion or accepts another GET_PC —
                   a hardware question outside this model.

Usage: python3 scripts/emi_interleave_model.py [--steps N]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field


@dataclass
class SocState:
    # raw window layer
    raw_ok: bool = True
    # worker bracket flags (0x939a1008+42/43) — bracketed, restored on exit
    worker_flags_held: bool = False
    # ESPIM async-in-flight (0x939a1008+29)
    obj29: bool = False
    # generic-pending (0x939a41b2 bit 0) and its expected opcode
    generic_pending: bool = False
    expected_opcode: int | None = None
    # mailbox slot: busy bits in 0x06000504, response retained in packet SRAM
    mailbox_busy: bool = False
    retained_opcode: int | None = None
    # outcomes
    oem12_failures: int = 0
    packet_status5: int = 0
    packet_ok: int = 0
    oem12_ok: int = 0
    misroutes: int = 0


def soc_oem12_read(s: SocState, inj: set[str]) -> None:
    """OEM-12 handler 0x93975260: flags bracket, chunked read, status 5."""
    if s.worker_flags_held:
        return  # single execution context serializes callers
    s.worker_flags_held = True
    if not s.raw_ok:
        s.oem12_failures += 1
    else:
        s.oem12_ok += 1
    s.worker_flags_held = False  # bracket release on ALL exits (proved)


def soc_packet_submit(s: SocState, opcode: int, inj: set[str]) -> None:
    """Packet send: busy-check, write block, doorbell, set generic-pending."""
    if not s.raw_ok:
        s.packet_status5 += 1  # raw layer fails -> immediate status 5
        return
    if s.mailbox_busy:
        s.packet_status5 += 0
        # busy returns 0xa, not 5; recorded via oem12-style refusal below
        return
    s.mailbox_busy = True
    s.generic_pending = True  # set only after successful send (0x93978fc0)
    s.expected_opcode = opcode
    # EC executes (not modeled in detail); on raw_wedge the EC never sees it


def ec_respond(s: SocState, inj: set[str]) -> None:
    """EC completes the pending mailbox request: matching opcode retained."""
    if not s.raw_ok or not s.generic_pending:
        return
    if "gp_latch" in inj:
        return  # this request's response never arrives; flag stays set
    s.retained_opcode = s.expected_opcode


def ec_producer_push(s: SocState, opcode: int, inj: set[str]) -> None:
    """Background producer (SCI 0xe2 -> opcode 0x12) writes response block.

    Unsolicited: can overwrite a retained completion while the SoC-side
    consumer has not yet drained it (the 05 09 / 12 00 00 anomaly).
    """
    if not s.raw_ok:
        return
    s.retained_opcode = opcode  # retained completion in packet SRAM


def soc_consume_response(s: SocState, inj: set[str]) -> None:
    """Dispatcher 0x9397940c -> consumer 0x939791c0 (clears gp first)."""
    if not s.generic_pending:
        return
    s.generic_pending = False  # cleared at consumer entry (0x93979204)
    s.mailbox_busy = False
    if s.retained_opcode is None:
        return  # response never arrived: submitter already saw its timeout
    if s.retained_opcode != s.expected_opcode:
        s.misroutes += 1  # no opcode matching in the dispatcher
    else:
        s.packet_ok += 1


def syndromes(s: SocState, n_oem: int, n_pkt: int) -> list[str]:
    out = []
    if s.packet_status5 >= n_pkt and s.oem12_failures >= n_oem:
        out.append("SYNDROME_LINK")
    if s.misroutes > 0:
        out.append("SYNDROME_MISROUTE")
    return out


def run(inj: set[str], steps: list[str]) -> SocState:
    s = SocState()
    if "raw_wedge" in inj:
        s.raw_ok = False
    for op in steps:
        if op == "oem12":
            soc_oem12_read(s, inj)
        elif op == "obj29_set":  # bracketed in firmware: set then cleared
            s.obj29 = True        # set 0x93968da8 ...
            s.obj29 = False       # ... cleared 0x93968de0 on every return
        elif op.startswith("pkt"):
            soc_packet_submit(s, int(op[3:], 16), inj)
        elif op == "ec_respond":
            ec_respond(s, inj)
        elif op == "push12":
            ec_producer_push(s, 0x12, inj)
        elif op == "consume":
            soc_consume_response(s, inj)
    return s


SCENARIOS = [
    ("healthy", set(),
     ["pkt07", "ec_respond", "consume", "oem12", "oem12"]),
    ("observed_0509_anomaly", set(),
     ["pkt05", "push12", "consume", "pkt05", "ec_respond", "consume"]),
    ("raw_wedge_after_crossing_read", {"raw_wedge"},
     ["oem12", "pkt04", "pkt04", "oem12"]),
    ("generic_pending_latch_only", {"gp_latch"},
     ["pkt04", "ec_respond", "push12", "consume", "oem12", "pkt04"]),
    ("obj29_bracket_excluded", {"obj29_latch"},
     ["obj29_set", "oem12", "oem12", "pkt04", "pkt04"]),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    for name, inj, steps in SCENARIOS:
        s = run(inj, steps)
        n_oem = sum(1 for x in steps if x == "oem12")
        n_pkt = sum(1 for x in steps if x.startswith("pkt"))
        syn = syndromes(s, n_oem, n_pkt)
        print(f"{name:32s} oem12 ok/fail {s.oem12_ok}/{s.oem12_failures}"
              f"  pkt ok/s5 {s.packet_ok}/{s.packet_status5}"
              f"  misroutes {s.misroutes}  -> {', '.join(syn) or 'none'}")


if __name__ == "__main__":
    main()

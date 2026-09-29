#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Summarize a completed passive EC capture; never accesses the hardware."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from statistics import median
import sys

LIMITS = ("pl1", "pl2", "syspl1", "syspl2", "syspl3", "syspl4")
PUBLICATION = re.compile(
    r"version=(0305080000) source_mw=(\d+(?:,\d+){5}) rtc_bcd=([0-9a-f]{12})"
)
SAMPLE = re.compile(
    r"(\d+) start_ns=(\d+) duration_ns=(\d+) status=([0-9a-f]{2}),([0-9a-f]{2}) "
    r"opcode=([0-9a-f]{2}) packet_changed=([01]) rtc_bcd=([0-9a-f]{12})"
)
TRACE_HEADER = "schema=1 samples=24 submitted_packets=0 inner_status_available=0"
TRACE_HEADER_V2 = ("schema=2 samples=24 submitted_packets=0 inner_status_available=0 "
                   "version_checks=52 budget_pairs=2")
FOOTER = "probe_unloaded=1 oem_unbound=1 boot_unchanged=1"


def rtc_time(value: str) -> datetime:
    """The traced mirror stores BCD second, minute, hour, day, month, year."""
    if any(c not in "0123456789" for c in value):
        raise ValueError("RTC mirror is not valid BCD")
    second, minute, hour, day, month, year = (int(value[i:i + 2]) for i in range(0, 12, 2))
    return datetime(2000 + year, month, day, hour, minute, second)


def analyze(text: str) -> dict:
    lines = text.splitlines()
    version, boot_id = 1, None
    if lines and lines[0] == "capture_format=ec-publication-v2":
        if len(lines) != 35 or not re.fullmatch(
                r"boot_id=[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", lines[3]):
            raise ValueError("not a complete v2 capture with a boot ID")
        version, boot_id = 2, lines.pop(3).removeprefix("boot_id=")
    if len(lines) != 34 or lines[0] != f"capture_format=ec-publication-v{version}":
        raise ValueError("not a complete ec-publication-v1/v2 capture")
    if not lines[1].startswith("captured_at="):
        raise ValueError("missing capture timestamp")
    captured_at = datetime.fromisoformat(lines[1].removeprefix("captured_at="))
    if captured_at.utcoffset() is None:
        raise ValueError("capture timestamp needs a UTC offset")
    if not re.fullmatch(r"module_sha256=[0-9a-f]{64}", lines[2]):
        raise ValueError("missing module digest")
    if (lines[3] != "capture_part=initial" or lines[5] != "capture_part=trace"
            or lines[6] != (TRACE_HEADER_V2 if version == 2 else TRACE_HEADER)
            or lines[31] != "capture_part=final"
            or lines[33] != FOOTER):
        raise ValueError("capture framing or cleanup evidence invalid")
    publications = []
    times = []
    for line in (lines[4], lines[32]):
        match = PUBLICATION.fullmatch(line)
        if not match:
            raise ValueError("invalid publication record or EC version canary")
        values = [int(value) for value in match[2].split(",")]
        if any(value > 1_000_000 for value in values):
            raise ValueError("implausible source budget")
        timestamp = rtc_time(match[3])
        publications.append({"source_mw": dict(zip(LIMITS, values)),
                             "rtc": timestamp.isoformat()})
        times.append(timestamp)
    observations = []
    sample_times = []
    previous_end = -1
    for index, line in enumerate(lines[7:31]):
        match = SAMPLE.fullmatch(line)
        if not match or int(match[1]) != index:
            raise ValueError("missing, duplicate or malformed sample")
        start, duration = int(match[2]), int(match[3])
        if duration <= 0 or start <= previous_end:
            raise ValueError("invalid or overlapping sample times")
        previous_end = start + duration
        changed = bool(int(match[7]))
        if index == 0 and changed:
            raise ValueError("first sample cannot establish a packet change")
        observations.append({"start_ns": start, "duration_ns": duration,
                             "status_before": int(match[4], 16),
                             "status_after": int(match[5], 16),
                             "opcode": match[6], "packet_changed": changed})
        sample_times.append(rtc_time(match[8]))
    elapsed = (previous_end - observations[0]["start_ns"]) / 1_000_000_000
    if elapsed < 7:
        raise ValueError("capture too short to assess time publication")
    times = [times[0], *sample_times, times[1]]
    changes = sum(a != b for a, b in zip(times, times[1:]))
    regressed = any(b < a for a, b in zip(times, times[1:]))
    advancing = changes > 0 and not regressed
    packet_changes = sum(row["packet_changed"] for row in observations)
    zero_budgets = all(value == 0 for p in publications for value in p["source_mw"].values())
    nonzero_budgets = all(value > 0 for p in publications for value in p["source_mw"].values())
    if regressed:
        assessment = "rtc_regressed_or_incoherent"
    elif zero_budgets and changes == 0 and packet_changes:
        assessment = "zero_budgets_static_time_mailbox_changes"
    elif nonzero_budgets and advancing:
        assessment = "budgets_and_time_publication_observed"
    else:
        assessment = "inconclusive"
    return {
        "schema_version": 2,
        "capture_version": version,
        "boot_id": boot_id,
        "assessment": assessment,
        "captured_at": lines[1].removeprefix("captured_at="),
        "module_sha256": lines[2].removeprefix("module_sha256="),
        "elapsed_seconds": elapsed,
        "first_sample_ns": observations[0]["start_ns"],
        "last_sample_end_ns": previous_end,
        "sample_duration_ms": {
            "median": median(row["duration_ns"] for row in observations) / 1_000_000,
            "max": max(row["duration_ns"] for row in observations) / 1_000_000,
        },
        "initial": publications[0],
        "final": publications[1],
        "rtc_changes": changes,
        "rtc_advancing": advancing,
        "packet_changes": packet_changes,
        "observed_opcodes": sorted({row["opcode"] for row in observations}),
        "busy_status_observations": sum(
            bool(row[key] & 3) for row in observations
            for key in ("status_before", "status_after")
        ),
        "cleanup_verified": True,
        "inner_ec_read_status_available": False,
        "version_checks_bracket_samples": version == 2,
        "paired_budget_reads_agree": True if version == 2 else None,
        "limitations": [
            "Sequential samples can miss traffic and do not identify request/response ownership.",
            ("Bracketing canaries and agreeing budget pairs cannot prove individual read success "
             "or atomicity; repeated masked failures and firmware housekeeping errors remain possible."
             if version == 2 else
             "Legacy separate canaries do not validate individual budget/time reads."),
            "The firmware discards inner EC read status and may service background events after a read.",
            "This measures publication, not physical RTC, task state, GPU health or recovery.",
        ],
    }


def compare(before: dict, after: dict) -> dict:
    """Compare completed captures from one host boot, without inferring continuity."""
    if not before["boot_id"] or before["boot_id"] != after["boot_id"]:
        raise ValueError("comparison requires v2 captures with the same boot ID")
    seconds = (datetime.fromisoformat(after["captured_at"]) -
               datetime.fromisoformat(before["captured_at"])).total_seconds()
    if seconds <= 0:
        raise ValueError("baseline must precede the current capture")
    if after["first_sample_ns"] <= before["last_sample_end_ns"]:
        raise ValueError("comparison samples overlap or are out of order")
    rtc_seconds = (datetime.fromisoformat(after["final"]["rtc"]) -
                   datetime.fromisoformat(before["final"]["rtc"])).total_seconds()
    progress = (before["assessment"] == "zero_budgets_static_time_mailbox_changes" and
                after["assessment"] == "budgets_and_time_publication_observed")
    persistent = (before["assessment"] == after["assessment"] ==
                  "zero_budgets_static_time_mailbox_changes" and rtc_seconds == 0)
    return {
        "assessment": ("publication_progress_observed" if progress else
                       "publication_fault_observed_again" if persistent else "inconclusive"),
        "same_host_boot": True,
        "capture_start_separation_seconds": seconds,
        "sample_gap_seconds": (after["first_sample_ns"] - before["last_sample_end_ns"]) / 1_000_000_000,
        "final_rtc_difference_seconds": rtc_seconds,
        "final_source_budgets_changed": before["final"]["source_mw"] != after["final"]["source_mw"],
        "limitations": [
            "Endpoint observations do not establish continuous behavior between captures.",
            "An unchanged host boot ID does not exclude an EC reset or a service restart.",
            "Publication progress alone does not establish GPU performance recovery.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--baseline", type=Path,
                        help="compare an earlier v2 capture from the same host boot")
    args = parser.parse_args(argv)
    try:
        report = analyze(args.capture.read_text(encoding="ascii"))
        if args.baseline:
            before = analyze(args.baseline.read_text(encoding="ascii"))
            report["comparison"] = compare(before, report)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

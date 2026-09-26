#!/usr/bin/env python3
"""Read DGX Spark SPBM power telemetry and set restrictive power limits."""

from __future__ import annotations

import argparse
import errno
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

HWMON_NAME = "dgx_spbm_power"
LIMITS = ("pl1", "pl2", "syspl1", "syspl2")
SUMMARY = (
    ("system", "sys_total"),
    ("package", "soc_pkg"),
    ("gpu", "gpu"),
    ("cpu_p", "cpu_p"),
    ("cpu_e", "cpu_e"),
)
MICROWATTS_PER_WATT = 1_000_000
MICROWATTS_PER_MILLIWATT = 1_000
# Matches the driver's plausibility bound; the driver enforces the real range.
MAX_WATTS = Decimal(1000)

LOG = logging.getLogger("dgx-power-control")


def find_hwmon_device(hwmon_root: Path) -> Path:
    matches = []
    for candidate in sorted(hwmon_root.glob("hwmon*")):
        try:
            name = (candidate / "name").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if name == HWMON_NAME:
            matches.append(candidate)
    if len(matches) != 1:
        raise RuntimeError(f"expected one {HWMON_NAME!r} hwmon device, found {len(matches)}")
    return matches[0]


def labelled_channels(device: Path, kind: str) -> dict[str, Path]:
    """Map labels to attribute prefixes; hwmon channel numbers are not an ABI here."""
    channels: dict[str, Path] = {}
    for label_path in sorted(device.glob(f"{kind}*_label")):
        label = label_path.read_text(encoding="ascii").strip()
        if label in channels:
            raise RuntimeError(f"duplicate {kind} label {label!r}")
        channels[label] = device / label_path.name.removesuffix("_label")
    return channels


def read_integer(path: Path) -> int | None:
    """Return None when the driver reports no data (an unpublished limit)."""
    try:
        return int(path.read_text(encoding="ascii").strip())
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return None
        raise


def attribute(prefix: Path, name: str) -> Path:
    return prefix.with_name(f"{prefix.name}_{name}")


def limit_channel(device: Path, name: str) -> Path:
    if name not in LIMITS:
        raise ValueError(f"limit must be one of {', '.join(LIMITS)}")
    channels = labelled_channels(device, "power")
    if name not in channels:
        raise RuntimeError(f"power limit {name!r} is not exposed by the driver")
    return channels[name]


def parse_watts(text: str) -> int:
    """Return whole milliwatts as microwatts, the driver's accepted resolution."""
    try:
        watts = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number of watts: {text!r}") from exc
    if not watts.is_finite() or watts <= 0 or watts > MAX_WATTS:
        raise ValueError("watts must be positive and finite")
    milliwatts = watts * 1000
    if milliwatts != milliwatts.to_integral_value():
        raise ValueError("watts may have at most three decimal places")
    return int(milliwatts) * MICROWATTS_PER_MILLIWATT


def write_cap(prefix: Path, microwatts: int) -> None:
    attribute(prefix, "cap").write_text(f"{microwatts}\n", encoding="ascii")


def set_limit(device: Path, name: str, microwatts: int) -> None:
    prefix = limit_channel(device, name)
    write_cap(prefix, microwatts)
    observed = read_integer(attribute(prefix, "cap"))
    if observed != microwatts:
        raise RuntimeError(f"{name} readback mismatch: wrote {microwatts}, read {observed}")


def restore(device: Path, names: tuple[str, ...]) -> list[str]:
    """Clear OS limits; return any limit whose NVIDIA value cannot be compared."""
    unverified = []
    for name in names:
        prefix = limit_channel(device, name)
        write_cap(prefix, 0)
        nvidia = read_integer(attribute(prefix, "cap_max"))
        observed = read_integer(attribute(prefix, "cap"))
        if nvidia is None:
            unverified.append(name)
        elif observed != nvidia:
            raise RuntimeError(f"{name} did not return to NVIDIA's {nvidia} uW; read {observed}")
    return unverified


def watts(microwatts: int | None) -> str:
    if microwatts is None:
        return "unpublished"
    return f"{microwatts / MICROWATTS_PER_WATT:.2f}W"


def status(device: Path) -> str:
    power = labelled_channels(device, "power")
    temps = labelled_channels(device, "temp")
    readings = [
        f"{field}={watts(read_integer(attribute(power[label], 'input')))}"
        for field, label in SUMMARY
    ]
    hottest = max(read_integer(attribute(prefix, "input")) or 0 for prefix in temps.values())
    prochot = (device / "prochot").read_text(encoding="ascii").strip()
    writable = all(attribute(power[name], "cap").stat().st_mode & 0o200 for name in LIMITS)
    lines = [
        " ".join(readings)
        + f" hottest={hottest / 1000:.1f}C prochot={prochot}"
        + f" control={'available' if writable else 'disabled'}"
    ]
    for name in LIMITS:
        prefix = power[name]
        cap = read_integer(attribute(prefix, "cap"))
        nvidia = read_integer(attribute(prefix, "cap_max"))
        lines.append(
            f"{name} average={watts(read_integer(attribute(prefix, 'input')))} "
            f"cap={watts(cap)} nvidia={watts(nvidia)} "
            f"floor={watts(read_integer(attribute(prefix, 'cap_min')))}"
            + (" restricted" if cap is not None and nvidia is not None and cap < nvidia else "")
        )
    return "\n".join(lines)


def diagnose(device: Path) -> dict:
    """Read applied limits and ceilings; never infer EC health from GPU clocks."""
    limits = {}
    for name in LIMITS:
        prefix = limit_channel(device, name)
        applied = read_integer(attribute(prefix, "cap"))
        nvidia = read_integer(attribute(prefix, "cap_max"))
        if applied is None or applied <= 0 or applied > 1_000_000_000:
            raise RuntimeError(f"{name} applied limit unavailable or implausible")
        if nvidia is not None and (nvidia <= 0 or nvidia > 1_000_000_000):
            raise RuntimeError(f"{name} NVIDIA limit implausible")
        limits[name] = {"applied_uw": applied, "nvidia_uw": nvidia}
    unpublished = [name for name, row in limits.items() if row["nvidia_uw"] is None]
    above = [name for name, row in limits.items()
             if row["nvidia_uw"] is not None and row["applied_uw"] > row["nvidia_uw"]]
    below = [name for name, row in limits.items()
             if row["nvidia_uw"] is not None and row["applied_uw"] < row["nvidia_uw"]]
    fallback = len(unpublished) == len(LIMITS) and tuple(
        limits[name]["applied_uw"] for name in LIMITS
    ) == (20_000_000, 20_000_000, 30_000_000, 30_000_000)
    if unpublished:
        assessment = "nvidia_limits_unpublished"
        message = ("NVIDIA limits are missing. Automatic mode cannot republish EC data. "
                   "No restart-free firmware repair has been demonstrated.")
    elif above:
        assessment = "applied_above_nvidia"
        message = "Applied limits exceed their observed ceilings; repeat the sequential capture."
    elif below:
        assessment = "applied_below_nvidia"
        message = "NVIDIA limits are published, with lower applied caps; their owner is not identified."
    else:
        assessment = "limits_match_nvidia"
        message = "Applied limits match NVIDIA's published ceilings; this does not establish EC or GPU health."
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "assessment": assessment,
        "message": message,
        "unpublished_limits": unpublished,
        "applied_above_nvidia": above,
        "applied_below_nvidia": below,
        "matches_observed_fallback": fallback,
        "limits": limits,
    }


def format_diagnosis(report: dict) -> str:
    lines = [f"assessment={report['assessment']}", report["message"]]
    if report["matches_observed_fallback"]:
        lines.append("Matches the observed 20/20 W package and 30/30 W system fallback pattern.")
    for name, row in report["limits"].items():
        lines.append(f"{name} cap={watts(row['applied_uw'])} nvidia={watts(row['nvidia_uw'])}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hwmon-root",
        type=Path,
        default=Path("/sys/class/hwmon"),
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show power, temperature, and limits")
    diagnosis = subparsers.add_parser("diagnose", help="read and assess firmware limits without writing")
    diagnosis.add_argument("--json", action="store_true", help="emit a structured capture")
    set_parser = subparsers.add_parser("set-limit", help="lower one limit below NVIDIA's")
    set_parser.add_argument("limit", help=", ".join(LIMITS))
    set_parser.add_argument("watts", help="whole milliwatts, for example 100 or 99.5")
    automatic = subparsers.add_parser("automatic", help="restore NVIDIA's limits")
    automatic.add_argument("limits", nargs="*", metavar="LIMIT", help="default: all four")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        device = find_hwmon_device(args.hwmon_root)
        if args.command == "status":
            print(status(device))
            return 0
        if args.command == "diagnose":
            report = diagnose(device)
            print(json.dumps(report, indent=2) if args.json else format_diagnosis(report))
            return 2 if report["unpublished_limits"] or report["applied_above_nvidia"] else 0
        if args.command == "set-limit":
            set_limit(device, args.limit, parse_watts(args.watts))
            return 0
        if args.command == "automatic":
            for name in restore(device, tuple(args.limits) or LIMITS):
                LOG.warning("%s cleared, but NVIDIA's limit is unpublished; not verified", name)
            return 0
    except (OSError, RuntimeError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())

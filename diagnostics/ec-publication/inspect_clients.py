#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Inventory Linux clients sharing the EC partition without sending firmware requests."""
import json
from pathlib import Path
import re
import sys

OEM_UUID = "884a63a0-3285-4120-83aa-eec008a0a546"


def inspect(endpoints: Path) -> dict:
    if not endpoints.is_dir():
        raise ValueError("FF-A device directory is absent")
    clients = []
    for device in sorted(endpoints.iterdir()):
        alias = device / "modalias"
        if not alias.exists():
            continue
        value = alias.read_text(encoding="ascii").strip()
        match = re.fullmatch(r"arm_ffa:8003:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", value)
        if not match:
            if value.startswith("arm_ffa:8003:"):
                raise ValueError(f"malformed EC partition alias at {device.name}")
            continue
        binding = device / "driver"
        clients.append({"device": device.name, "uuid": match[1],
                        "driver": binding.readlink().name if binding.is_symlink() else None,
                        "oem_endpoint": match[1] == OEM_UUID})
    oem_count = sum(client["oem_endpoint"] for client in clients)
    bound = sum(client["driver"] is not None for client in clients)
    assessment = ("oem_endpoint_missing_or_ambiguous" if oem_count != 1 else
                  "collection_refused_bound_partition_clients" if bound else
                  "client_check_passed_other_preflight_checks_required")
    return {"schema_version": 1, "assessment": assessment, "partition": "8003",
            "bound_clients": bound, "clients": clients, "firmware_requests": 0,
            "limitations": ["Bindings can change after this inventory.",
                            "An unbound Linux endpoint does not exclude firmware background traffic."]}


def main() -> int:
    try:
        print(json.dumps(inspect(Path("/sys/bus/arm_ffa/devices")), indent=2))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay fixed read cases from SoC 2.155.11 in emulated memory only.

Requires unicorn==2.1.4 and an independently obtained, hash-matching capsule.
The bus, completion data and housekeeping are simulated. This establishes
wrapper control flow, not the cause of a physical transport failure.
"""
import argparse
import hashlib
import json
from pathlib import Path

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import (
    UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2,
    UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_SP,
)

CAPSULE_SHA256 = "0985b848b1708421a399935b7f9b1afb5469588db6f26bd2953c6013f3db70ff"
IMAGE_OFFSET = 0xA08F0F
IMAGE_SIZE = 0x41100
IMAGE_VA = 0x93942000
SCRATCH = 0x1000000
OUTPUT = SCRATCH + 0x1000
STOP = SCRATCH + 0xF000
REGS = (UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Replay:
    def __init__(self, image, *, failed_chunk=None, housekeeping_status=0):
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.uc.mem_map(0x93940000, 0x70000)
        self.uc.mem_write(IMAGE_VA, image)
        self.uc.mem_map(SCRATCH, 0x10000)
        self.failed_chunk = failed_chunk
        self.housekeeping_status = housekeeping_status
        self.chunks = []
        self.housekeeping_calls = 0
        self.mode = "read"
        self.configuration = {0x10: 0x1115}
        self.configuration_writes = []
        self.uc.hook_add(UC_HOOK_CODE, self.instruction)

    def done(self, value=0):
        self.uc.reg_write(UC_ARM64_REG_X0, value & 0xFFFFFFFFFFFFFFFF)
        self.uc.reg_write(UC_ARM64_REG_PC, self.uc.reg_read(UC_ARM64_REG_LR))

    def instruction(self, uc, pc, size, user_data):
        args = [uc.reg_read(reg) for reg in REGS]
        if self.mode == "configuration":
            if pc == 0x939691F0:  # Simulated GET_CONFIGURATION.
                uc.mem_write(args[1], self.configuration.get(args[0], 0).to_bytes(4, "little"))
                self.done()
            elif pc == 0x93969084:  # Record SET_CONFIGURATION without bus access.
                self.configuration[args[0]] = args[1]
                self.configuration_writes.append([args[0], args[1]])
                self.done()
            elif pc in (0x93946A78, 0x93968BAC, 0x93968D74, 0x93965498):
                # Logging, controller init/reset and clock selection are stubbed.
                self.done()
            else:
                require(0x9396DC10 <= pc < 0x9396DF9C,
                        f"unexpected configuration execution at {pc:#x}")
            return
        if pc == 0x93969C60:  # Physical transfer boundary is never executed.
            address, length, destination = args
            self.chunks.append([address, length])
            require(0 < length <= 64, "unexpected physical read size")
            if len(self.chunks) == self.failed_chunk:
                self.done(-111)
            else:
                uc.mem_write(destination, bytes([0xA0 + len(self.chunks)]) * length)
                self.done()
        elif pc == 0x9395B684:  # memcpy in emulated memory.
            destination, source, length = args
            uc.mem_write(destination, bytes(uc.mem_read(source, length)))
            self.done(destination)
        elif pc == 0x9396C0A0:  # Housekeeping can itself fail.
            self.housekeeping_calls += 1
            self.done(self.housekeeping_status)
        else:
            require(0x9396C80C <= pc <= 0x9396CA68,
                    f"unexpected read execution at {pc:#x}")

    def call(self, start, *args, stop=STOP):
        self.uc.reg_write(UC_ARM64_REG_SP, SCRATCH + 0xE000)
        self.uc.reg_write(UC_ARM64_REG_LR, STOP)
        for register, value in zip(REGS, args):
            self.uc.reg_write(register, value)
        self.uc.emu_start(start, stop, count=10000)
        require(self.uc.reg_read(UC_ARM64_REG_PC) == stop, "instruction budget exhausted")
        value = self.uc.reg_read(UC_ARM64_REG_X0) & 0xFFFFFFFF
        return value if value < 0x80000000 else value - 0x100000000


def replay(image):
    results = []
    cases = [
        ("wide_success", 0x06000714, 81, None, 0),
        ("wide_first_chunk_failure", 0x06000714, 81, 1, 0),
        ("wide_second_chunk_failure", 0x06000714, 81, 2, 0),
        ("wide_housekeeping_failure", 0x06000714, 81, None, -7),
        ("version", 0x06000760, 5, None, 0),
        ("budgets", 0x06000714, 24, None, 0),
        ("time", 0x06000788, 6, None, 0),
        ("packet", 0x06000800, 8, None, 0),
        ("status", 0x06000504, 1, None, 0),
    ]
    for name, address, length, failed_chunk, housekeeping_status in cases:
        model = Replay(image, failed_chunk=failed_chunk,
                       housekeeping_status=housekeeping_status)
        result = model.call(0x9396C80C, address, length, OUTPUT)
        expected = [[address, min(length, 64)]]
        if length > 64 and failed_chunk != 1:
            expected.append([address + 64, length - 64])
        require(model.chunks == expected, f"{name}: unexpected chunking")
        require(result == (-1 if failed_chunk else housekeeping_status),
                f"{name}: unexpected result")
        require(model.housekeeping_calls == int(failed_chunk is None),
                f"{name}: unexpected housekeeping")
        data = bytes(model.uc.mem_read(OUTPUT, length))
        expected_data = bytearray(length)
        offset = 0
        for index, (_, count) in enumerate(expected, 1):
            if index != failed_chunk:
                expected_data[offset:offset + count] = bytes([0xA0 + index]) * count
            offset += count
        require(data == expected_data, f"{name}: unexpected output preservation")
        results.append({"case": name, "read_calls": model.chunks,
                        "wrapper_status": result,
                        "housekeeping_calls": model.housekeeping_calls,
                        "synthetic_bytes_copied": sum(value != 0 for value in data)})

    model = Replay(image)
    model.mode = "configuration"
    model.call(0x9396DC10, stop=0x9396DF9C)
    writes = [value for address, value in model.configuration_writes if address == 0x10]
    require(writes == [0x7115], "unexpected channel-0 configuration")
    return {"capsule_sha256": CAPSULE_SHA256, "hardware_accesses": 0,
            "scope": "original wrapper instructions; simulated bus, data and housekeeping",
            "incident_cause_established": False,
            "initialization_channel0_write": hex(writes[0]),
            "requested_maximum_read_bytes": 4096,
            "requested_maximum_payload_bytes": 64,
            "live_configuration_verified": False, "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capsule", type=Path)
    args = parser.parse_args()
    capsule = args.capsule.read_bytes()
    if hashlib.sha256(capsule).hexdigest() != CAPSULE_SHA256:
        parser.error("capsule SHA-256 does not match SoC 2.155.11")
    image = capsule[IMAGE_OFFSET:IMAGE_OFFSET + IMAGE_SIZE]
    require(len(image) == IMAGE_SIZE, "truncated image")
    print(json.dumps(replay(image), indent=2))


if __name__ == "__main__":
    main()

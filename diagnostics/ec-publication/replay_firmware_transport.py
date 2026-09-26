#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay SoC 2.155.11 transport instructions against a synthetic controller.

Requires unicorn==2.1.4 and the same independently obtained capsule as
replay_firmware_reads.py. No device, network, or live-memory interface exists.
The controller and EC are MODELS, not emulated hardware. In particular, the
retained-credit scenario is a hypothesis, not a reproduction of dgx1's cause.
"""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path

from unicorn.arm64_const import (
    UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
)

from replay_firmware_reads import (
    CAPSULE_SHA256, IMAGE_OFFSET, IMAGE_SIZE, OUTPUT, REGS, SCRATCH, STOP,
    Replay, require,
)

CONTEXT = 0x939A1008
DESCRIPTOR = 0x939A10F0
CONTROLLER = 0x16050000
READ = 0x9396C80C
HOUSEKEEPING = 0x9396C0A0
ALERT = 0x9396C308
WAIT_COMPLETION = 0x9396570C
GET_COMPLETION = 0x93969B1C


class TransportReplay(Replay):
    """Run the original manual-FIFO path; model its MMIO accessor results.

    Register meanings are inferred from the pinned instructions. We do not
    model hardware filtering, CRC, DMA, asynchronous interrupts, other channel
    traffic, or wall-clock timing. GET_STATUS uses ACCEPT without an appended
    completion. Empty FIFO reads return zero only in this model.
    """

    def __init__(self, image):
        self.registers = {offset: 0 for offset in
                          (0x24, 0x28, 0x30, 0x1C, 0x0C, 0x20, 0x2C)}
        self.registers[0x18] = 8
        self.tx = []
        self.rx = deque()
        self.transfers = []
        self.writes = []
        self.response = 8
        self.completion = None
        self.command_timeout = False
        self.completion_ready = True
        self.pending = None
        self.get_completion_response = 8
        self.hold_credit = False
        self.polls = 0
        self.empty_reads = 0
        super().__init__(image)
        # Original context initialization; one-time controller setup is stubbed.
        self.call(0x939658E0, CONTEXT, DESCRIPTOR)
        # Exercise real housekeeping even when only PC_AVAIL is asserted.
        self.uc.mem_write(0x939830E0, b"\x01")

    def launch(self):
        require(bool(self.tx), "command without TX data")
        opcode = self.tx[0] & 0xFF
        payload = b""
        if opcode == 2:  # PUT_NP, Memory Read32.
            require(len(self.tx) == 4 and self.tx[0] == 2,
                    "unexpected PUT_NP framing")
            length = self.tx[1] >> 4
            address = self.tx[2] | self.tx[3] << 16
            require(0 < length <= 64, "unexpected read length")
            require(self.tx[1] & 15 == 10, "unexpected request tag")
            self.transfers.append([opcode, address, length])
            if self.hold_credit and self.pending is not None:
                # HYPOTHESIS: one target slot stays occupied until GET_PC.
                # PUT_NP while NP_FREE=0 produces a modeled FATAL_ERROR.
                self.registers[0x18] = 3
            else:
                require(self.pending is None, "unmodeled overlapping request")
                payload = self.completion
                if payload is None:
                    payload = bytes([0x0F, 0xA0, length]) + bytes(range(1, length + 1))
                self.registers[0x18] = self.response
                if self.response == 1:
                    self.pending = payload
                if self.response not in (8, 2) or self.command_timeout:
                    payload = b""
        elif opcode == 0x25:  # GET_STATUS; no response modifier in this model.
            require(len(self.tx) == 1, "unexpected GET_STATUS framing")
            self.transfers.append([opcode])
            self.registers[0x18] = 8
        elif opcode == 1:  # GET_PC, consuming a previously deferred completion.
            require(len(self.tx) == 1 and self.pending is not None,
                    "GET_PC without a pending completion")
            require(self.completion_ready, "GET_PC before completion available")
            self.transfers.append([opcode])
            self.registers[0x18] = self.get_completion_response
            if self.get_completion_response == 8:
                payload, self.pending = self.pending, None
        else:
            raise RuntimeError(f"unmodeled command {opcode:#x}")
        self.tx.clear()
        # PC_FREE and VWIRE_FREE, optional NP_FREE and PC_AVAIL.
        self.registers[0x1C] = (
            5 | (0 if self.hold_credit and self.pending is not None else 2)
            | (16 if self.pending is not None and self.completion_ready else 0)
        )
        self.registers[0x28] = 0 if self.command_timeout else 1
        self.rx.extend(int.from_bytes(payload[i:i + 4].ljust(4, b"\0"), "little")
                       for i in range(0, len(payload), 4))

    def read_register(self, address):
        offset = address - CONTROLLER
        if offset == 0xF4:
            return 0x10 if not self.rx else 0
        if offset == 0x104:
            if self.rx:
                return self.rx.popleft()
            self.empty_reads += 1
            return 0  # Explicit assumption; actual empty-FIFO value is unknown.
        require(offset in self.registers, f"unmodeled MMIO read {address:#x}")
        if offset == 0x28:
            self.polls += 1
        return self.registers[offset]

    def write_register(self, address, value):
        offset = address - CONTROLLER
        self.writes.append([offset, value])
        if offset == 0xFC:
            self.tx.append(value)
        elif offset in (0x28, 0x30):
            self.registers[offset] &= ~value  # Modeled write-one-to-clear.
        elif offset in (4, 8, 0x0C, 0x1C, 0x20, 0xF4, 0x24, 0x2C):
            self.registers[offset] = value
            if offset == 0x0C and value & 1:
                self.launch()
        else:
            raise RuntimeError(f"unmodeled MMIO write {address:#x}={value:#x}")

    def instruction(self, uc, pc, size, user_data):
        if pc in (0x9395FD8C, 0x939655D8):
            self.done(self.read_register(uc.reg_read(REGS[0])))
        elif pc == 0x9395FD68:
            self.write_register(uc.reg_read(REGS[0]), uc.reg_read(REGS[1]))
            self.done()
        elif pc in (0x93946A78, 0x9394780C, 0x939608F8):
            # Log output, delay, and initial controller programming only.
            # Error cleanup at 0x9396035c is executed, not stubbed.
            self.done()
        elif pc == 0x9395B684:
            destination, source, length = [uc.reg_read(reg) for reg in REGS]
            uc.mem_write(destination, bytes(uc.mem_read(source, length)))
            self.done(destination)
        else:
            require(0x9395FD68 <= pc <= 0x93969FA8
                    or HOUSEKEEPING <= pc <= 0x9396C5AC
                    or READ <= pc <= 0x9396CA68,
                    f"unexpected execution at {pc:#x}")

    def call(self, start, *args, stop=STOP):
        self.uc.reg_write(UC_ARM64_REG_SP, SCRATCH + 0xE000)
        self.uc.reg_write(UC_ARM64_REG_LR, STOP)
        for register, value in zip(REGS, args):
            self.uc.reg_write(register, value)
        self.uc.emu_start(start, stop, count=2_000_000)
        require(self.uc.reg_read(UC_ARM64_REG_PC) == stop, "instruction budget exhausted")
        value = self.uc.reg_read(UC_ARM64_REG_X0) & 0xFFFFFFFF
        return value if value < 0x80000000 else value - 0x100000000

    def read_version(self, destination=OUTPUT):
        return self.call(READ, 0x06000760, 5, destination)

    def output(self, destination=OUTPUT):
        return bytes(self.uc.mem_read(destination, 5)).hex()


def replay(image):
    results = []
    for name, response, timeout in (
        ("normal", 8, False), ("fatal", 3, False),
        ("no_response", 0xFF, False), ("command_timeout", 8, True),
    ):
        model = TransportReplay(image)
        model.response, model.command_timeout = response, timeout
        status = model.call(READ, 0x06000714, 81, OUTPUT)
        expected = 0 if name == "normal" else -1
        require(status == expected, f"{name}: first read status")
        chunks = list(model.transfers)
        require(chunks == ([[2, 0x06000714, 64], [2, 0x06000754, 17]]
                           if expected == 0 else [[2, 0x06000714, 64]]),
                f"{name}: chunk sequence")
        require(model.polls == (5001 if timeout else len(chunks)),
                f"{name}: command-done polling")
        require(model.writes[-2:] == [[0x24, 0], [0x2C, 1]],
                f"{name}: interrupt settings not restored")
        model.response, model.command_timeout = 8, False
        require(model.read_version(OUTPUT + 0x100) == 0, f"{name}: subsequent read")
        require(model.output(OUTPUT + 0x100) == "0102030405", f"{name}: subsequent data")
        results.append({"case": name, "first_status": status,
                        "first_read_commands": chunks, "subsequent_read_status": 0})

    for name, response, completion, expected, empty in (
        ("short_completion", 8, "0fa001ee", "ee02030405", 0),
        ("unsuccessful_completion", 8, "0ea000", "0002030405", 0),
        ("wrong_tag", 8, "0f3005aabbccddee", "aabbccddee", 0),
        ("nonfatal_empty_fifo", 2, "", "0002030405", 1),
    ):
        model = TransportReplay(image)
        require(model.read_version() == 0 and model.output() == "0102030405",
                f"{name}: seed read")
        model.response, model.completion = response, bytes.fromhex(completion)
        status = model.read_version(OUTPUT + 0x100)
        require(status == 0 and model.output(OUTPUT + 0x100) == expected,
                f"{name}: unexpected validation behavior")
        require(model.empty_reads == empty, f"{name}: FIFO read count")
        results.append({"case": name, "wrapper_status": status,
                        "copied_bytes": expected, "empty_fifo_reads": empty})

    for name, response in (("deferred_normal", 8), ("deferred_get_pc_fatal", 3)):
        model = TransportReplay(image)
        require(model.read_version() == 0, f"{name}: seed read")
        model.response, model.get_completion_response = 1, response
        model.completion = bytes.fromhex("0fa005aabbccddee")
        status = model.read_version(OUTPUT + 0x100)
        expected = "aabbccddee" if response == 8 else "0102030405"
        require(status == 0 and model.output(OUTPUT + 0x100) == expected,
                f"{name}: result")
        require(model.transfers[-3:] == [[2, 0x06000760, 5], [0x25], [1]],
                f"{name}: deferred sequence")
        results.append({"case": name, "wrapper_status": status,
                        "copied_bytes": expected})

    # Causal, conditional example: same emulated software state throughout.
    model = TransportReplay(image)
    model.hold_credit, model.response, model.completion_ready = True, 1, False
    require(model.call(READ, 0x06000714, 81, OUTPUT) == -1, "late completion: timeout")
    require(model.transfers == [[2, 0x06000714, 64]] + [[0x25]] * 1001,
            "late completion: wait sequence")
    model.response, model.completion_ready = 8, True
    failures = [model.read_version() for _ in range(3)]
    require(failures == [-1, -1, -1], "late completion: persistent failures")
    background_start = len(model.transfers)
    require(model.call(HOUSEKEEPING) == 0, "late completion: housekeeping")
    model.call(ALERT)  # Void function; X0 is not a status result.
    require(model.transfers[background_start:] == [[0x25]],
            "late completion: background unexpectedly consumed completion")
    require(model.pending is not None and model.read_version() == -1,
            "late completion: background changed the failure")
    require(model.call(WAIT_COMPLETION, 4) == 0, "late completion: availability")
    require(model.call(GET_COMPLETION) == 0 and model.pending is None,
            "late completion: drain")
    require(model.read_version() == 0 and model.output() == "0102030405",
            "late completion: subsequent read did not recover")
    results.append({"case": "hypothetical_retained_credit",
                    "initial_timeout_status": -1,
                    "following_read_statuses": failures,
                    "after_background_status": -1, "after_modeled_drain_status": 0,
                    "target_credit_behavior_verified": False,
                    "live_recovery_verified": False})

    # The prefix preserves Response Modifier Enable, rather than forcing it on.
    configurations = []
    for initial in (0, 0x40000000):
        model = Replay(image)
        model.mode = "configuration"
        model.configuration[8] = initial
        model.call(0x9396DC10, stop=0x9396DF9C)
        written = model.configuration[8]
        require(written == initial | 0x90000000, "response modifier configuration")
        configurations.append({"initial_general_config": hex(initial),
                               "requested_general_config": hex(written)})

    return {"capsule_sha256": CAPSULE_SHA256, "hardware_accesses": 0,
            "scope": "original manual-FIFO instructions; synthetic controller and EC",
            "incident_cause_established": False, "live_configuration_verified": False,
            "assumptions": [
                "inferred register/FIFO semantics, without hardware error filtering",
                "GET_STATUS returns no appended completion; empty FIFO reads return zero",
                "no unrelated traffic or asynchronous interrupt interleaving",
                "retained-credit case assumes one target slot held until completion is drained",
                "target still accepts GET_PC after the modeled fatal responses",
                "logging, delays, initial controller setup and memcpy are stubbed",
            ], "configuration_cases": configurations, "cases": results}


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

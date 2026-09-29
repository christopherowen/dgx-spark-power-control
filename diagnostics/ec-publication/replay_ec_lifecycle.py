#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay fixed EC 3.5.8 publication paths in emulated memory only.

Requires unicorn==2.1.4 and a separately obtained matching capsule. This is
not a scheduler, a hardware model, or a callable interface to a physical EC.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
)

CAPSULE_SHA256 = "ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82"
IMAGE_OFFSET, IMAGE_VA, IMAGE_SIZE = 0xD9F, 0xC0000, 0x13000
WINDOW, STATE, INITIALIZED = 0x119000, 0x118468, 0x11A8F0
EVENT, STACK, STOP = 0x11A044, 0x127000, 0x127F00
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
LIMITS = [231000, 244000, 257000, 265000]
# Only reviewed functions/branches can execute. All addresses are image-pinned.
EXECUTABLE = (
    (0xC14D4, 0xC1534), (0xC1A60, 0xC1ADC), (0xC1B08, 0xC1B7C),
    (0xC2070, 0xC2088), (0xC2538, 0xC25B4), (0xC2760, 0xC27E8),
    (0xC3664, 0xC366E), (0xC36D4, 0xC3716), (0xC373C, 0xC374C),
    (0xC3758, 0xC3760), (0xC3E24, 0xC3E68), (0xC4268, 0xC427E),
    (0xC4288, 0xC42D4), (0xC4820, 0xC4916), (0xC5780, 0xC578A),
    (0xC583E, 0xC5842), (0xC5D6C, 0xC5D80),
    (0xC7CE4, 0xC7CEA), (0xC7CF0, 0xC7CF6), (0xC7CFC, 0xC7D02),
    (0xC7D08, 0xC7D0E), (0xC7D1C, 0xC7D34), (0xC7D38, 0xC7D52),
    (0xC7D58, 0xC7D72), (0xCC6A6, 0xCC6C8), (0xCC6C8, 0xCC6DA),
    (0xCCA92, 0xCCA96), (0xCCE46, 0xCCE48),
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Replay:
    def __init__(self, image):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.uc.mem_map(IMAGE_VA, 0x14000)
        self.uc.mem_write(IMAGE_VA, image)
        self.uc.mem_map(0x118000, 0x10000)
        # Startup copy at 0xcba1c: initialized data, including preserve table.
        self.uc.mem_write(0x118000, image[0x11854:0x12364])
        self.uc.mem_map(0x400F3000, 0x1000)  # In-memory eSPI mapping registers.
        self.uc.hook_add(UC_HOOK_CODE, self.instruction)
        self.trace = []
        self.events = 0
        self.wait_40_result = 0
        self.pltrst = 1
        self.pltrst_status = 0
        self.gpio_values = iter([1, 1, 1])
        self.rtc_status = 0
        self.rtc_bytes = bytes.fromhex("32310706260926")
        self.rtc_registers = {7: 0}
        self.initializations = 0
        self.system_publications = 0

    def read32(self, address):
        return struct.unpack("<I", self.uc.mem_read(address, 4))[0]

    def byte(self, address):
        return self.uc.mem_read(address, 1)[0]

    def done(self, value=0):
        self.uc.reg_write(UC_ARM_REG_R0, value & 0xFFFFFFFF)
        self.uc.reg_write(UC_ARM_REG_PC, self.uc.reg_read(UC_ARM_REG_LR))

    def instruction(self, uc, pc, size, user_data):
        a, b, c, d = [uc.reg_read(reg) for reg in REGS]
        if pc == 0xCFAE2:  # memset, emulator memory only.
            require(c <= 0x400, "unexpected memset size")
            uc.mem_write(a, bytes([b & 255]) * c)
            self.done(a)
        elif pc == 0xCF2DC:  # GPIO devices modeled ready.
            self.done(1)
        elif pc == 0xCC7CA:  # Three power-state input pins.
            self.done(next(self.gpio_values))
        elif pc == 0xC641C:
            require(a == 4, "unexpected virtual wire")
            uc.mem_write(b, bytes([self.pltrst]))
            self.done(self.pltrst_status)
        elif pc == 0xC6048:  # RTC I2C transaction boundary.
            sp = uc.reg_read(UC_ARM_REG_SP)
            require((a, b, d, self.read32(sp + 4)) == (0, 0x32, 1, 7),
                    "unexpected I2C transaction")
            if not self.rtc_status:
                uc.mem_write(self.read32(sp), self.rtc_bytes)
            self.trace.append({"rtc_read_status": self.rtc_status})
            self.done(self.rtc_status)
        elif pc == 0xCC3E6:
            require(a == 7, "unexpected RTC register read")
            self.done(self.rtc_registers[a])
        elif pc == 0xCC41A:
            require(a == 7, "unexpected RTC register write")
            self.rtc_registers[a] = b
            self.done()
        elif pc == 0xC5CDC:  # Board revision lookup, outside replay scope.
            self.done(0)
        elif pc == 0xC5E94:  # Select the branch that waits for boot-ready.
            self.done(0)
        elif pc in (0xCF98A, 0xCF990):
            require(a == EVENT, "unexpected event object")
            mask = b if pc == 0xCF98A else c
            self.events = (self.events & ~mask) | (b & mask)
            self.trace.append({"event_update_mask": mask, "events": self.events})
            self.done()
        elif pc == 0xCF994:
            require(a == EVENT and b in (1, 0x40) and c == 0,
                    "unexpected event wait")
            if b == 0x40:
                self.events |= self.wait_40_result
            result = self.events & b
            sp = uc.reg_read(UC_ARM_REG_SP)
            self.trace.append({"wait_mask": b, "result": result,
                               "timeout_ticks": self.read32(sp)})
            self.done(result)
        elif pc in (0xCF4E8, 0xCF57E, 0xCB5B8):
            operation = {0xCF4E8: "start", 0xCF57E: "suspend", 0xCB5B8: "resume"}[pc]
            self.trace.append({"thread_operation": operation, "thread": hex(a)})
            self.done()
        elif pc in (0xCF7BE, 0xCBDB0, 0xCB020, 0xCD022, 0xC35DC, 0xC3D14):
            # Work scheduling, GPIO output, follow-up setup and notification
            # are recorded, without executing a scheduler or interrupt path.
            self.trace.append({"stub": hex(pc), "args": [a, b, c, d]})
            self.done()
        else:
            require(any(start <= pc < end for start, end in EXECUTABLE),
                    f"unexpected execution at {pc:#x}")
            if pc == 0xC4820:
                self.initializations += 1
            elif pc == 0xC3E24:
                self.system_publications += 1

    def call(self, address, *args):
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        for reg, value in zip(REGS, args):
            self.uc.reg_write(reg, value)
        self.uc.emu_start(address | 1, STOP, count=20000)
        require(self.uc.reg_read(UC_ARM_REG_PC) == STOP, "instruction budget exhausted")
        return self.uc.reg_read(UC_ARM_REG_R0)

    def observe(self, old, requested, pins, *, initialized=0):
        self.uc.mem_write(STATE, bytes([old, requested]))
        self.uc.mem_write(INITIALIZED, bytes([initialized]))
        self.gpio_values = iter(pins)
        self.call(0xC1A60)

    def snapshot(self, name):
        return {"case": name, "initializations": self.initializations,
                "system_publications": self.system_publications,
                "budgets_mw": list(struct.unpack("<6I", self.uc.mem_read(WINDOW + 0x114, 24))),
                "internal_system_mw": list(struct.unpack("<4I", self.uc.mem_read(0x11AA1B, 16))),
                "rtc_mirror_hex": bytes(self.uc.mem_read(WINDOW + 0x188, 6)).hex(),
                "thermal_init_flags": list(self.uc.mem_read(0x11AA97, 3)),
                "publication_enabled": self.byte(0x11AA93),
                "events": self.events, "initialized": self.byte(INITIALIZED),
                "trace": self.trace.copy()}


def replay(image):
    cases = []
    model = Replay(image)
    states = []
    for bits in range(8):
        model.gpio_values = iter([(bits >> shift) & 1 for shift in (2, 1, 0)])
        status = model.call(0xC2538, STACK - 0x100)
        require(status in (0, 0xFFFFFFF2), "unexpected state decode status")
        states.append(model.byte(STACK - 0x100) if status == 0 else None)
    require(states == [5, 3, None, 2, None, 6, None, 2], "unexpected state table")

    model = Replay(image)
    model.uc.mem_write(WINDOW, b"\xa5" * 0x200)
    model.uc.mem_write(0x118C00, b"\xa5" * 0x400)
    model.uc.mem_write(0x11AA97, b"\x01\x01\x01")
    preserved = [struct.unpack("<H", model.uc.mem_read(0x11892C + 4 * i, 2))[0]
                 for i in range(16)]
    require(preserved == [0x1A0, *range(0x170, 0x178), *range(0x1B5, 0x1B9),
                          0x19A, 0x19B, 0x19C], "unexpected preserve table")
    model.call(0xC4820)
    require(all(model.byte(WINDOW + offset) == 0xA5 for offset in preserved if offset != 0x19C),
            "preserved metadata lost")
    # Status at 0x19c is preserved initially, then refreshed by 0xc4288.
    require(model.byte(WINDOW + 0x19C) == 0, "status was not refreshed")
    require(not any(model.uc.mem_read(WINDOW + 0x114, 24)), "budgets survived init")
    require(not any(model.uc.mem_read(0x118C00, 0x400)), "packet survived init")
    require(bytes(model.uc.mem_read(0x11AA97, 3)) == b"\0\0\0", "thermal flags survived init")
    require(bytes(model.uc.mem_read(WINDOW + 0x188, 6)).hex() == "323107260926",
            "initial RTC publication missing")
    cases.append(model.snapshot("window_init_preserves_metadata_and_clears_budgets"))

    # The state observer initializes only on a transition, not merely because
    # the initialized flag is clear. All callbacks for states 2/3 execute.
    for name, old, requested, initialized, expected in (
        ("observer_enter_state2", 3, 2, 0, 1),
        ("observer_unchanged_state2", 2, 2, 0, 0),
        ("observer_request_mismatch", 3, 5, 0, 0),
        ("observer_already_initialized", 3, 2, 1, 0),
    ):
        model = Replay(image)
        model.observe(old, requested, [1, 1, 1], initialized=initialized)
        require(model.initializations == expected, name)
        require(model.system_publications == 0 and model.events == 0, name)
        require(not any(model.uc.mem_read(WINDOW + 0x114, 24)), name)
        cases.append(model.snapshot(name))

    for name, event_result, expected_event, expected_init in (
        ("power_wait_event40", 0x40, 0, 0),
        ("power_wait_poll_pltrst", 0, 1, 1),
    ):
        model = Replay(image)
        model.wait_40_result = event_result
        require(model.call(0xC1B08) == 0, name)
        require(model.events == expected_event and model.initializations == expected_init, name)
        require(model.system_publications == 0, name)
        cases.append(model.snapshot(name))

    model = Replay(image)
    model.call(0xC2760)
    require(model.events == 0x40 and model.system_publications == 1, "PLTRST publication")
    require(struct.unpack("<4I", model.uc.mem_read(WINDOW + 0x11C, 16)) == tuple(LIMITS),
            "unexpected nominal system budgets")
    require(any(item.get("wait_mask") == 1 and item["result"] == 0 for item in model.trace),
            "missing boot-ready timeout")
    cases.append(model.snapshot("pltrst_work_publishes_after_boot_ready_timeout"))

    # A sequential counterexample, not a claim about physical scheduling:
    # first publish, observe leaving state 2, then observe reentry to state 2.
    model.observe(2, 2, [0, 0, 1], initialized=1)
    require(model.byte(INITIALIZED) == 0, "leaving state 2 did not invalidate init")
    model.observe(3, 2, [1, 1, 1])
    require(model.initializations == 2 and model.system_publications == 1,
            "unexpected publisher rerun")
    require(not any(model.uc.mem_read(WINDOW + 0x114, 24)), "budgets survived reinit")
    require(struct.unpack("<4I", model.uc.mem_read(0x11AA1B, 16)) == tuple(LIMITS),
            "internal system budgets unexpectedly cleared")
    require(bytes(model.uc.mem_read(0x11AA97, 3)) == b"\0\0\0", "thermal init flags")
    require(model.byte(0x11AA93) == 1, "publication disabled")
    cases.append(model.snapshot("observer_reentry_erases_published_system_limits"))

    for name, level, status in (("pltrst_low_clears_boot_ready", 0, 0),
                                ("pltrst_read_error_requeues", 1, -5)):
        model = Replay(image)
        model.events = 1
        model.pltrst, model.pltrst_status = level, status
        model.call(0xC2760)
        require(model.initializations == model.system_publications == 0, name)
        require(model.events == (1 if status else 0), name)
        require(any(item.get("stub") == "0xcb020" for item in model.trace) == bool(status), name)
        cases.append(model.snapshot(name))

    for name, status, rtc_bytes, expected in (
        ("rtc_success_advances_mirror", 0, "33310706260926", "333107260926"),
        ("rtc_returned_error_marks_ff", -5, "33310706260926", "ffffffffffff"),
    ):
        model = Replay(image)
        model.call(0xC4820)
        model.rtc_status, model.rtc_bytes = status, bytes.fromhex(rtc_bytes)
        model.call(0xC14D4)
        require(bytes(model.uc.mem_read(WINDOW + 0x188, 6)).hex() == expected, name)
        cases.append(model.snapshot(name))

    model = Replay(image)
    threads = []
    for address in range(0xD07D4, 0xD0840, 12):
        thread, grouped, name = struct.unpack("<3I", model.uc.mem_read(address, 12))
        label = bytes(model.uc.mem_read(name, 80)).split(b"\0")[0].decode("ascii")
        threads.append({"address": hex(thread), "name": label, "power_group": bool(grouped)})
    for entry, operation in ((0xC7D1C, "start"), (0xC7D38, "suspend"), (0xC7D58, "resume")):
        model.call(entry)
        actual = [item["thread"] for item in model.trace if item.get("thread_operation") == operation]
        expected = [row["address"] for row in threads if operation == "start" or row["power_group"]]
        require(actual == expected, f"unexpected {operation} group")
    cases.append(model.snapshot("thread_group_selection"))
    return {"capsule_sha256": CAPSULE_SHA256, "hardware_accesses": 0,
            "scope": "original bounded EC instructions; simulated I2C, GPIO, events and scheduling boundaries",
            "live_cause_established": False, "live_recovery_established": False,
            "power_state_decode": states, "threads": threads, "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capsule", type=Path)
    args = parser.parse_args()
    capsule = args.capsule.read_bytes()
    if hashlib.sha256(capsule).hexdigest() != CAPSULE_SHA256:
        parser.error("capsule SHA-256 does not match EC 3.5.8")
    image = capsule[IMAGE_OFFSET:IMAGE_OFFSET + IMAGE_SIZE]
    require(len(image) == IMAGE_SIZE, "truncated image")
    print(json.dumps(replay(image), indent=2))


if __name__ == "__main__":
    main()

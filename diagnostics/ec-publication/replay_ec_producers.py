#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay fixed EC 3.5.8 producer gates, I2C wrappers and event orderings.

Uses original instructions with explicitly simulated mutex, I2C, notification
and RTOS boundaries. No physical EC access or complete RTOS scheduling model.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC

from replay_ec_lifecycle import (
    CAPSULE_SHA256, IMAGE_OFFSET, IMAGE_SIZE, WINDOW, STATE, EVENT,
    STACK, STOP, REGS, Replay, require,
)

RTC_EVENT = 0x118B04
BUS_TABLE = 0x118694
PRODUCER_CODE = (
    (0xC153C, 0xC1542), (0xC188C, 0xC18C2), (0xC1BF0, 0xC1BF6),
    (0xC3134, 0xC3156), (0xC3764, 0xC37B6),
    (0xC3870, 0xC38A8), (0xC38B0, 0xC38F8),
    (0xC3A28, 0xC3AFC), (0xC3CD0, 0xC3CE2),
    (0xC3D4C, 0xC3D7C), (0xC3DC0, 0xC3E1C), (0xC5D4C, 0xC5D64),
    (0xC5D90, 0xC5D9C), (0xC5DA0, 0xC5DA4), (0xC5DA8, 0xC5DB2),
    (0xC5FE0, 0xC6044), (0xC6048, 0xC60B8),
    (0xCC3E6, 0xCC40A), (0xCC41A, 0xCC434),
    (0xCC894, 0xCC8AE), (0xCCF60, 0xCCF74),
)


class ProducerReplay(Replay):
    def __init__(self, image):
        super().__init__(image)
        self.event_bits = {EVENT: 0, RTC_EVENT: 0}
        self.held_mutexes = set()
        self.i2c_status = 0
        self.notify_result = 1
        self.block_notification = False
        self.stop_at = set()
        self.halted = None
        self.i2c_calls = []
        self.mutex_calls = []
        self.notifications = []
        self.query_events = []
        self.work_submissions = []

    def halt(self, reason, **details):
        self.halted = {"reason": reason, **details}
        self.uc.emu_stop()

    def instruction(self, uc, pc, size, user_data):
        a, b, c, d = [uc.reg_read(reg) for reg in REGS]
        if pc in self.stop_at:
            self.halt("reviewed_boundary", pc=hex(pc))
        elif pc == 0xCABA0:  # Mutex lock, reached through the real wrapper.
            require(c == d == 0xFFFFFFFF, "unexpected I2C lock timeout")
            require(a in [BUS_TABLE + 0x1C * bus + 8 for bus in range(3)],
                    "unreviewed mutex")
            self.mutex_calls.append({"operation": "lock_forever", "address": hex(a)})
            if a in self.held_mutexes:
                self.halt("simulated_mutex_wait", mutex=hex(a))
            else:
                self.held_mutexes.add(a)
                self.done()
        elif pc == 0xCAC90:
            require(a in self.held_mutexes, "unlock without modeled ownership")
            self.held_mutexes.remove(a)
            self.mutex_calls.append({"operation": "unlock", "address": hex(a)})
            self.done()
        elif pc == 0xCE7B6:  # Device API transfer boundary; no physical I2C.
            require(a in (0xCFDD4, 0xCFDC0, 0xCFDAC) and c in (1, 2),
                    "unexpected I2C transfer")
            messages = []
            for index in range(c):
                pointer, length = struct.unpack("<2I", uc.mem_read(b + 12 * index, 8))
                flags = self.byte(b + 12 * index + 8)
                require(0 < length <= 7, "unexpected I2C message length")
                messages.append((pointer, length, flags))
            register = self.byte(messages[0][0])
            self.i2c_calls.append({"device": hex(a), "target": hex(d),
                                   "register": register, "messages": c,
                                   "status": self.i2c_status})
            if not self.i2c_status and c == 2:
                destination, length, flags = messages[1]
                require(flags == 7, "unexpected read flags")
                payload = self.rtc_bytes if register == 0 and length == 7 else bytes(length)
                uc.mem_write(destination, payload)
            self.done(self.i2c_status)
        elif pc == 0xC3D14:  # Budget stores occur before this notification.
            require(a == 8, "unexpected producer notification")
            self.notifications.append(a)
            if self.block_notification:
                self.halt("simulated_notification_wait")
            else:
                self.done(self.notify_result)
        elif pc in (0xCF98A, 0xCF990):
            require(a in self.event_bits, "unexpected event object")
            mask = b if pc == 0xCF98A else c
            self.event_bits[a] = (self.event_bits[a] & ~mask) | (b & mask)
            self.done()
        elif pc == 0xCF994:
            require(a in self.event_bits and c == 0, "unexpected event wait")
            result = self.event_bits[a] & b
            if not result:
                timeout = bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_SP), 8))
                require(timeout == b"\xff" * 8, "unexpected bounded event wait")
                self.halt("simulated_event_wait", event=hex(a), mask=b)
            else:
                self.done(result)
        elif pc == 0xC30C4:
            require(a in (0xE0, 0xE1, 0xE2), "unexpected query event")
            self.query_events.append(a)
            self.done()
        elif pc == 0xCAF98:
            require(a == 0x1184D0, "unexpected work submission")
            self.work_submissions.append({"work": hex(a),
                                          "rtc_events_already_posted": self.event_bits[RTC_EVENT]})
            self.done()
        elif pc in (0xCD09E, 0xCD0C6, 0xC6500, 0xC6598, 0xCB798):
            # GPIO/PWM setup and sleep are outside this model.
            self.trace.append({"stub": hex(pc), "args": [a, b, c, d]})
            self.done()
        elif any(start <= pc < end for start, end in PRODUCER_CODE):
            # These include functions stubbed by the lifecycle-only replay:
            # I2C wrappers and RTC register access execute here in full.
            pass
        else:
            super().instruction(uc, pc, size, user_data)

    def run(self, address, *args, stop_at=()):
        self.halted = None
        self.stop_at = set(stop_at)
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        for register, value in zip(REGS, args):
            self.uc.reg_write(register, value)
        self.uc.emu_start(address | 1, STOP, count=50000)
        require(self.halted is not None or self.uc.reg_read(UC_ARM_REG_PC) == STOP,
                "instruction budget exhausted")

    def snapshot(self, name):
        return {"case": name, "halted": self.halted,
                "package_limits_mw": [self.read32(WINDOW + 0x114), self.read32(WINDOW + 0x118)],
                "rtc_mirror_hex": bytes(self.uc.mem_read(WINDOW + 0x188, 6)).hex(),
                "fan_floor_bytes": list(self.uc.mem_read(WINDOW + 0x190, 4)),
                "thermal_init_flags": list(self.uc.mem_read(0x11AA97, 3)),
                "events": {hex(k): v for k, v in self.event_bits.items()},
                "i2c_calls": self.i2c_calls, "mutex_calls": self.mutex_calls,
                "held_mutexes": sorted(hex(x) for x in self.held_mutexes),
                "notifications": self.notifications, "query_events": self.query_events,
                "work_submissions": self.work_submissions}


def replay(image):
    results = []
    model = ProducerReplay(image)
    topology = []
    for bus, expected_device in enumerate((0xCFDD4, 0xCFDC0, 0xCFDAC)):
        row = BUS_TABLE + 0x1C * bus
        device = model.read32(row)
        require(device == expected_device, "unexpected I2C device table")
        configuration = model.read32(device + 4)
        controller = model.read32(configuration + 4)
        require(model.read32(configuration) == bus, "unexpected I2C controller index")
        require(controller == 0x40004000 + bus * 0x400, "unexpected I2C controller")
        topology.append({"logical_bus": bus, "device": hex(device),
                         "controller": hex(controller), "mutex": hex(row + 8)})

    for name, status, notify in (
        ("thermal_normal", 0, 1), ("thermal_i2c_errors_return", -5, 1),
        ("thermal_notification_fails", 0, 0),
        ("thermal_i2c_and_notification_errors", -5, 0),
    ):
        model = ProducerReplay(image)
        model.i2c_status, model.notify_result = status, notify
        model.uc.mem_write(STATE, b"\x02")
        model.uc.mem_write(0x11AA93, b"\x01")
        model.run(0xC3A28, 0xD0848, stop_at=(0xC3AF8,))
        result = model.snapshot(name)
        require(result["package_limits_mw"] == [140000, 142000], name)
        require(result["fan_floor_bytes"] == [255] * 4, name)
        require(result["thermal_init_flags"] == [1, notify, 1], name)
        require(len(model.i2c_calls) == 10 and not model.held_mutexes, name)
        require(len(model.mutex_calls) == 20, name)
        require(model.notifications == ([8, 8] if notify else [8]), name)
        require({call["device"] for call in model.i2c_calls} == {"0xcfdac"}, name)
        require({call["address"] for call in model.mutex_calls} == {"0x1186d4"}, name)
        results.append(result)

    # Original sensor-init flag is 0x11aa99, not the data-structure base
    # 0x11a8f9. Execute window reinitialization between two bounded passes.
    for name, i2c_status in (("sensor_init_retried_after_window_reset", 0),
                             ("sensor_errors_retried_after_window_reset", -5)):
        model = ProducerReplay(image)
        model.i2c_status = i2c_status
        model.uc.mem_write(STATE, b"\x02")
        model.uc.mem_write(0x11AA93, b"\x01")
        model.run(0xC3A28, 0xD0848, stop_at=(0xC3AF8,))
        require(model.byte(0x11AA99) == 1 and len(model.i2c_calls) == 10, name)
        context = model.uc.context_save()
        stack = bytes(model.uc.mem_read(STACK - 0x100, 0x100))
        model.run(0xC4820)
        require(bytes(model.uc.mem_read(0x11AA97, 3)) == b"\0\0\0", name)
        require(not any(model.uc.mem_read(WINDOW + 0x114, 24)), name)
        before_retry = len(model.i2c_calls)
        # Supply arrival at the next loop head. Later policy work and the
        # scheduler remain outside the model; thread-local registers survive.
        model.uc.context_restore(context)
        model.uc.mem_write(STACK - 0x100, stack)
        model.halted = None
        model.stop_at = {0xC3AF8}
        model.uc.emu_start(0xC3A9D, STOP, count=50000)
        require(model.halted is not None and model.halted.get("pc") == "0xc3af8", name)
        retry_calls = model.i2c_calls[before_retry:]
        result = model.snapshot(name)
        require(len(retry_calls) == 10 and all(x["device"] == "0xcfdac" for x in retry_calls), name)
        require(result["package_limits_mw"] == [140000, 142000], name)
        require(result["fan_floor_bytes"] == [255] * 4, name)
        require(model.byte(0x11AA99) == 1, name)
        result["sensor_writes_after_window_reset"] = len(retry_calls)
        results.append(result)

    for name, state, enabled in (("thermal_wrong_state", 3, 1),
                                 ("thermal_publication_disabled", 2, 0)):
        model = ProducerReplay(image)
        model.uc.mem_write(STATE, bytes([state]))
        model.uc.mem_write(0x11AA93, bytes([enabled]))
        model.run(0xC3A28, 0xD0848, stop_at=(0xC3AF8, 0xC3B68))
        result = model.snapshot(name)
        require(result["package_limits_mw"] == [0, 0], name)
        require(result["fan_floor_bytes"] == ([0] * 4 if state != 2 else [255] * 4), name)
        results.append(result)

    for name, held_bus, expect_block in (("thermal_bus2_mutex_held", 2, True),
                                        ("thermal_bus0_mutex_held", 0, False)):
        model = ProducerReplay(image)
        model.held_mutexes.add(BUS_TABLE + 0x1C * held_bus + 8)
        model.uc.mem_write(STATE, b"\x02")
        model.uc.mem_write(0x11AA93, b"\x01")
        model.run(0xC3A28, 0xD0848, stop_at=(0xC3AF8,))
        result = model.snapshot(name)
        require((model.halted["reason"] == "simulated_mutex_wait") == expect_block, name)
        require(result["package_limits_mw"] == ([0, 0] if expect_block else [140000, 142000]), name)
        require(len(model.i2c_calls) == (0 if expect_block else 10), name)
        results.append(result)

    model = ProducerReplay(image)
    model.block_notification = True
    model.uc.mem_write(STATE, b"\x02")
    model.uc.mem_write(0x11AA93, b"\x01")
    model.run(0xC3A28, 0xD0848)
    result = model.snapshot("thermal_wait_inside_notification")
    require(result["package_limits_mw"] == [140000, 142000], "stores precede notification")
    require(result["fan_floor_bytes"] == [0] * 4, "fan initialization passed blocked notification")
    results.append(result)

    for name, held_bus, status in (("rtc_bus0_mutex_held", 0, 0),
                                   ("rtc_bus2_mutex_held", 2, 0),
                                   ("rtc_error_releases_mutex", None, -5)):
        model = ProducerReplay(image)
        if held_bus is not None:
            model.held_mutexes.add(BUS_TABLE + 0x1C * held_bus + 8)
        model.i2c_status = status
        model.uc.mem_write(WINDOW + 0x188, bytes.fromhex("313107260926"))
        model.run(0xC14D4)
        result = model.snapshot(name)
        expected = "313107260926" if held_bus == 0 else (
            "ffffffffffff" if status else "323107260926")
        require(result["rtc_mirror_hex"] == expected, name)
        require(len(model.i2c_calls) == (0 if held_bus == 0 else 1), name)
        expected_held = set() if held_bus is None else {BUS_TABLE + 0x1C * held_bus + 8}
        require(model.held_mutexes == expected_held, "RTC mutex cleanup changed")
        results.append(result)

    # Explicit ordering, not a full RTOS simulation: the query passes boot-ready,
    # PLTRST work clears it, the timer posts RTC work, and RTC reaches its gate.
    # Preserve the query's register/stack context to show its next real iteration.
    for name, entry, argument, first_event in (
        ("temperature_query_runs_while_rtc_waits", 0xC3870, 0xD0844, 0xE0),
        ("power_query_runs_while_rtc_waits", 0xC38B0, 0xD0840, 0xE2),
    ):
        model = ProducerReplay(image)
        model.event_bits[EVENT] = 1
        model.uc.mem_write(STATE, b"\x02")
        model.uc.mem_write(0x11AA93, b"\x01")
        model.run(entry, argument, stop_at=(0xCC8B4,))
        require(model.query_events[0] == first_event, name)
        query_context = model.uc.context_save()
        query_stack = bytes(model.uc.mem_read(STACK - 0x100, 0x100))
        model.pltrst = 0
        model.run(0xC2760)
        require(model.event_bits[EVENT] == 0, "PLTRST did not clear boot-ready")
        model.run(0xC3CD0)
        require(model.event_bits[RTC_EVENT] == 2, "timer did not post RTC event")
        require(model.work_submissions == [{"work": "0x1184d0", "rtc_events_already_posted": 2}],
                "RTC event depends on workqueue execution")
        model.run(0xC188C, stop_at=(0xC18B8,))
        require(model.halted == {"reason": "simulated_event_wait", "event": hex(EVENT), "mask": 1}, name)
        rtc_wait = model.halted
        before = len(model.query_events)
        model.uc.context_restore(query_context)
        model.uc.mem_write(STACK - 0x100, query_stack)
        model.done()  # Supplied return from the query's sleep boundary.
        model.halted = None
        model.stop_at = {0xCC8B4}
        model.uc.emu_start(model.uc.reg_read(UC_ARM_REG_PC) | 1, STOP, count=10000)
        require(model.halted is not None and model.halted["reason"] == "reviewed_boundary", name)
        require(len(model.query_events) > before and model.query_events[before] == first_event, name)
        result = model.snapshot(name)
        result["rtc_wait"] = rtc_wait
        results.append(result)

    return {"capsule_sha256": CAPSULE_SHA256, "hardware_accesses": 0,
            "scope": "bounded producer instructions with simulated I2C, mutex, event and notification boundaries",
            "i2c_topology": topology,
            "assumptions": [
                "GPIO and PWM setup succeed; the modeled board selector returns zero",
                "mutex ownership and release are supplied; scheduler and owner progress are not modeled",
                "I2C returns a supplied status and synthetic RTC bytes, without executing the controller driver",
                "notification returns a supplied value or stops at a hypothetical wait",
                "event waits return matching supplied bits or stop; no interrupt timing is inferred",
                "thermal execution stops before the later sensor/policy and lock paths at 0xc3af8",
                "window-reset retry supplies arrival at the next thermal loop head with preserved thread context",
                "query interleaving supplies sleep completion and preserved thread context; state 2 and enable remain set",
            ],
            "live_cause_established": False, "live_recovery_established": False,
            "cases": results}


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

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay EC mailbox waits with simulated ownership and status transitions.

Requires unicorn==2.1.4 and the pinned EC 3.5.8 capsule. This has no hardware
interface. It does not discover the live mutex owner or emulate RTOS scheduling.
"""
import argparse
import hashlib
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_PC

from replay_ec_producers import ProducerReplay
from replay_ec_lifecycle import (
    CAPSULE_SHA256, IMAGE_OFFSET, IMAGE_SIZE, REGS, STACK, STOP, STATE, require,
)

MAILBOX_MUTEX = 0x118A28
CODE = (
    (0xC3D14, 0xC3D46), (0xC496C, 0xC4994),
    (0xC49B0, 0xC4A46), (0xC4A4C, 0xC4A82),
    (0xC4CEC, 0xC4D2E), (0xC6734, 0xC6748),
    (0xC6784, 0xC67CE), (0xCCA96, 0xCCA9E),
)


class MailboxReplay(ProducerReplay):
    def __init__(self, image):
        super().__init__(image)
        self.uc.mem_map(0x400F0000, 0x2000)
        self.thread = "caller"
        self.owner = None
        self.depth = 0
        self.max_depth = 0
        self.polls = []
        self.sleeps = 0
        self.pause_after_sleep = None
        self.clear_after_sleep = None
        self.host_actions = []
        self.locks = []

    def status_address(self, channel):
        require(0 <= channel <= 3, "unreviewed channel")
        base = self.read32(0xD07A4 + channel * 4)
        require(base == 0x400F0800 + channel * 0x400, "unexpected channel table")
        return base + 0x104

    def set_status(self, channel, value):
        self.uc.mem_write(self.status_address(channel), bytes([value]))

    def supplied_data_consumption(self, channel):
        # Supplied hardware consequence of a correctly sized host DATA read.
        # The host mapping, access width and transaction are not executed here.
        address = self.status_address(channel)
        self.uc.mem_write(address, bytes([self.byte(address) & ~1]))
        self.host_actions.append({"action": "supplied_data_consumption",
                                  "channel": channel, "mutex_depth_after": self.depth})

    def instruction(self, uc, pc, size, user_data):
        a, b, c, d = [uc.reg_read(reg) for reg in REGS]
        if pc == 0xCABA0 and a == MAILBOX_MUTEX:
            require(c == d == 0xFFFFFFFF, "mailbox acquisition is not forever")
            self.locks.append({"operation": "lock_forever", "thread": self.thread,
                               "owner_before": self.owner, "depth_before": self.depth})
            if self.owner is not None and self.owner != self.thread:
                self.halt("simulated_mutex_wait", mutex=hex(a), owner=self.owner)
            else:
                self.owner = self.thread
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)
                self.done()
        elif pc == 0xCAC90 and a == MAILBOX_MUTEX:
            require(self.owner == self.thread and self.depth > 0, "invalid modeled unlock")
            self.depth -= 1
            self.locks.append({"operation": "unlock", "thread": self.thread,
                               "depth_after": self.depth})
            if not self.depth:
                self.owner = None
            self.done()
        elif pc == 0xC6734:
            require(b == 1, "unexpected mailbox status mask")
            self.polls.append({"channel": a, "status": self.byte(self.status_address(a)),
                               "mutex_depth": self.depth})
            # Execute the original indexed status read and bit test.
        elif pc == 0xC6374:
            require((a, b, c, d) == (4, 100, 1, 1), "unexpected virtual-wire wait")
            self.done(0)  # Explicitly supplied successful PLTRST handshake.
        elif pc in (0xCB798, 0xCF2D2) and self.depth:
            require((pc == 0xCB798 and a == 0x148 and b == 0)
                    or (pc == 0xCF2D2 and a == 10000), "unexpected polling delay")
            self.sleeps += 1
            if self.clear_after_sleep == self.sleeps:
                self.supplied_data_consumption(self.polls[-1]["channel"])
            if self.pause_after_sleep == self.sleeps:
                self.halt("supplied_owner_pause", owner=self.owner, mutex_depth=self.depth)
            else:
                self.done()
        elif any(start <= pc < end for start, end in CODE):
            # In particular, execute C3D14 instead of the producer replay's stub.
            pass
        else:
            super().instruction(uc, pc, size, user_data)

    def wait_channel(self, channel, ticks, busy_wait=False):
        self.uc.mem_write(STACK, int(busy_wait).to_bytes(4, "little"))
        self.run(0xC49B0, channel, 0, ticks & 0xFFFFFFFF, ticks >> 32)

    def result(self, name):
        result = self.snapshot(name)
        value = self.uc.reg_read(REGS[0])
        result.update({"return_value": None if self.halted else (
            value if value < 0x80000000 else value - 0x100000000),
            "mailbox_owner": self.owner, "mutex_depth": self.depth,
            "max_mutex_depth": self.max_depth, "status_polls": len(self.polls),
            "poll_channels": sorted({p["channel"] for p in self.polls}),
            "poll_status_values": sorted({p["status"] for p in self.polls}),
            "polling_delays": self.sleeps, "host_actions": self.host_actions.copy()})
        return result


def replay(image):
    cases = []
    for name, channel, ticks, busy_wait, expected_delays in (
        ("rtc_busy_status_timeout", 1, 0x18000, False, 300),
        ("budget_busy_status_timeout", 2, 0xCCD, False, 11),
        ("ucsi_busy_status_timeout", 1, 0x148, True, 2),
    ):
        model = MailboxReplay(image)
        model.set_status(channel, 1)
        if busy_wait:
            model.uc.mem_write(STACK, b"\x01\0\0\0")
            model.run(0xC4CEC, channel, 4, ticks, 0)
        else:
            model.wait_channel(channel, ticks)
        result = model.result(name)
        require(result["return_value"] == -11 and model.depth == 0, name)
        require(model.sleeps == expected_delays and len(model.polls) == expected_delays + 1, name)
        require(model.max_depth == (3 if busy_wait else 2), name)
        cases.append(result)

    model = MailboxReplay(image)
    model.set_status(1, 8)  # Value in saved dgx3 channel-1 status observations.
    model.wait_channel(1, 0x18000)
    require(model.depth == model.sleeps == 0 and len(model.polls) == 1, "idle channel waited")
    cases.append(model.result("saved_status_08_does_not_wait"))

    model = MailboxReplay(image)
    model.set_status(1, 1)
    model.clear_after_sleep = 3
    model.wait_channel(1, 0x18000)
    require(model.depth == 0 and model.sleeps == 3, "supplied consumption did not unblock running owner")
    require(model.host_actions[0]["mutex_depth_after"] == 1, "host action released software mutex")
    cases.append(model.result("running_owner_finishes_after_supplied_consumption"))

    model = MailboxReplay(image)
    model.owner, model.depth, model.max_depth = "other_task", 1, 1
    model.wait_channel(1, 0x18000)
    require(model.halted["reason"] == "simulated_mutex_wait" and not model.polls,
            "finite status timeout bounded mutex acquisition")
    cases.append(model.result("held_mutex_blocks_before_status_timeout"))

    model = MailboxReplay(image)
    model.owner, model.depth, model.max_depth = "other_task", 1, 1
    model.uc.mem_write(STATE, b"\x02")
    model.uc.mem_write(0x11AA93, b"\x01")
    model.run(0xC3A28, 0xD0848)
    result = model.result("thermal_waits_on_shared_mailbox_after_package_stores")
    require(model.halted["reason"] == "simulated_mutex_wait", "thermal did not reach mailbox mutex")
    require(result["package_limits_mw"] == [140000, 142000] and result["fan_floor_bytes"] == [0] * 4,
            "unexpected publication ordering")
    cases.append(result)

    model = MailboxReplay(image)
    model.run(0xC3D4C)
    require(model.uc.reg_read(REGS[0]) == 1 and model.depth == 0, "notification failed")
    require(model.read32(0x400F1100) == 8, "notification not written to channel 2")
    cases.append(model.result("package_notification_releases_recursive_mutex"))

    # Supply an owner pause inside the polling delay, then a host consumption.
    # Preserve its execution context while a second task attempts the same lock.
    model = MailboxReplay(image)
    model.thread = "owner_task"
    model.set_status(1, 1)
    model.pause_after_sleep = 1
    model.wait_channel(1, 0x18000)
    require(model.depth == 1 and model.halted["reason"] == "supplied_owner_pause", "owner not paused")
    context = model.uc.context_save()
    stack = bytes(model.uc.mem_read(STACK - 0x100, 0x100))
    model.supplied_data_consumption(1)
    model.thread = "waiter_task"
    model.wait_channel(2, 0xCCD)
    require(model.halted["reason"] == "simulated_mutex_wait" and model.depth == 1,
            "consuming data resumed a stopped owner")
    cases.append(model.result("consuming_data_does_not_release_paused_owner"))

    model.thread = "owner_task"
    model.uc.context_restore(context)
    model.uc.mem_write(STACK - 0x100, stack)
    model.pause_after_sleep = None
    model.halted = None
    model.done()  # Supply return from sleep after explicitly resuming the owner.
    model.uc.emu_start(model.uc.reg_read(UC_ARM_REG_PC) | 1, STOP, count=50000)
    require(model.uc.reg_read(UC_ARM_REG_PC) == STOP and model.depth == 0, "resumed owner did not unlock")
    model.thread = "waiter_task"
    model.wait_channel(2, 0xCCD)
    require(model.halted is None and model.depth == 0, "waiter could not acquire released mutex")
    cases.append(model.result("supplied_owner_resume_allows_waiter_progress"))

    return {"capsule_sha256": CAPSULE_SHA256, "hardware_accesses": 0,
            "mutex_address": hex(MAILBOX_MUTEX), "live_owner_identified": False,
            "live_recovery_established": False,
            "assumptions": [
                "recursive mutex ownership is modeled; original RTOS scheduler and mutex implementation are not run",
                "status bytes and host-consumption changes are supplied in emulated memory",
                "host DATA-read mapping, byte width, transport and interrupt effects are not executed",
                "PLTRST read and handshake succeed in the notification scenario",
                "sleep and busy-wait delays return immediately unless explicitly paused",
                "no scenario establishes a live owner, stall, hardware read side effect or repair",
            ], "cases": cases}


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

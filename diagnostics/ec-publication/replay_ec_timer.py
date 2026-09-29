#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay EC timer initialization and periodic ordering without hardware.

Requires unicorn==2.1.4 and the pinned EC 3.5.8 capsule. Other startup hooks,
the timeout queue, tick source, event delivery and work submission are modeled.
This does not execute the scheduler or establish the live timer's state.
"""
import argparse
import hashlib
import json
from pathlib import Path

from unicorn.arm_const import UC_ARM_REG_SP

from replay_ec_producers import ProducerReplay, RTC_EVENT
from replay_ec_lifecycle import CAPSULE_SHA256, IMAGE_OFFSET, IMAGE_SIZE, REGS, require

TIMER = 0x119368
CALLBACK = 0xC3CD1
PERIOD = 0x8000
CODE = (
    (0xCA7EC, 0xCA814), (0xCA818, 0xCA83A),
    (0xC3CE8, 0xC3D0C), (0xCF7BE, 0xCF7D6),
    (0xCBDB0, 0xCBE1E), (0xCBCD0, 0xCBDAC),
)
OTHER_STAGE4_HOOKS = (0xC26F4, 0xCD36A, 0xC6C4C, 0xC5B04, 0xC59CC)


class TimerReplay(ProducerReplay):
    def __init__(self, image):
        super().__init__(image)
        self.order = []
        self.startup_hooks = []
        self.schedules = []
        self.work_result = 0

    def instruction(self, uc, pc, size, user_data):
        a, b, c, d = [uc.reg_read(reg) for reg in REGS]
        if pc == 0xCA7EC and a == 3:
            self.order.append("supplied_stage3_return")
            self.done()
        elif pc in OTHER_STAGE4_HOOKS:
            self.startup_hooks.append(hex(pc))
            self.done()  # Explicitly supplied completion of other init hooks.
        elif pc in (0xCF99A, 0xCF340):
            self.done()  # Other boot setup, outside this replay.
        elif pc == 0xCF788:
            require(a == TIMER, "unexpected timeout cancellation")
            require(self.read32(TIMER) == 0, "modeled timeout must be unlinked")
            self.done(0)
        elif pc == 0xCF7BA:
            # Fixed tick value for the periodic absolute-deadline arithmetic.
            uc.reg_write(REGS[1], 0)
            self.done(0x10000)
        elif pc == 0xCBADC:
            require((a, b) == (TIMER, 0xCBCD1), "unexpected timeout queue insertion")
            self.order.append("queue_next_expiry")
            self.schedules.append({"timer": hex(a), "handler": hex(b),
                                   "timeout_words": [c, d]})
            self.done()  # No real queue or clock; only record this boundary.
        elif pc == 0xCF98A:
            require((a, b) == (RTC_EVENT, 2), "unexpected RTC event post")
            self.order.append("post_rtc_event_mask_2")
            super().instruction(uc, pc, size, user_data)
        elif pc == 0xCAF98:
            self.order.append("submit_separate_work")
            super().instruction(uc, pc, size, user_data)
            self.done(self.work_result)
        elif any(start <= pc < end for start, end in CODE):
            # Execute the real timer init/start/expiry paths instead of stubs.
            if pc == 0xC3CE8:
                self.startup_hooks.append(hex(pc))
            if pc == 0xCBDB0:
                sp = uc.reg_read(UC_ARM_REG_SP)
                require((a, c, d, self.read32(sp), self.read32(sp + 4))
                        == (TIMER, PERIOD, 0, PERIOD, 0), "unexpected initial/periodic timeout")
        else:
            super().instruction(uc, pc, size, user_data)


def replay(image):
    cases = []
    model = TimerReplay(image)
    require([model.read32(0xD0F7C + index * 4) for index in (4, 5)]
            == [0xCFC28, 0xCFC58], "unexpected stage-4 boundaries")
    require((model.read32(0xCFC40), model.read32(0xCFC44)) == (0xC3CE9, 0),
            "unexpected timer initialization entry")
    model.run(0xCA818, stop_at=(0xCA83A,))
    require(model.halted is not None and model.halted.get("pc") == "0xca83a",
            "startup did not reach the static-thread setup boundary")
    expected_hooks = ["0xc26f4", "0xcd36a", "0xc6c4c", "0xc3ce8", "0xc5b04", "0xc59cc"]
    require(model.startup_hooks == expected_hooks, "startup hook order changed")
    require(model.read32(TIMER + 0x20) == CALLBACK, "callback not installed")
    require(model.read32(TIMER + 0x28) == PERIOD and model.read32(TIMER + 0x2C) == 0,
            "period not stored")
    require(len(model.schedules) == 1 and model.schedules[0]["timeout_words"] == [PERIOD - 1, 0],
            "initial expiry not scheduled")
    cases.append({"case": "timer_initialized_before_static_thread_setup",
                  "startup_hooks": model.startup_hooks, "callback": hex(CALLBACK),
                  "period_ticks": model.read32(TIMER + 0x28), "schedules": model.schedules.copy()})

    for name, work_result in (("periodic_requeue_precedes_rtc_event", 0),
                              ("work_submission_error_does_not_cancel_periodic_requeue", -16)):
        model = TimerReplay(image)
        model.run(0xC3CE8)
        model.work_result = work_result
        model.order.clear()
        model.schedules.clear()
        # Supply two expiries of an unlinked timer; the queue itself is not run.
        for _ in range(2):
            model.run(0xCBCD0, TIMER)
        require(model.order == ["queue_next_expiry", "post_rtc_event_mask_2",
                                "submit_separate_work"] * 2, name)
        require(model.event_bits[RTC_EVENT] == 2 and len(model.schedules) == 2, name)
        require(model.read32(TIMER + 0x30) == 2, "expiry count did not advance")
        require(model.read32(TIMER + 0x28) == PERIOD, "period was changed")
        cases.append({"case": name, "work_result": work_result, "order": model.order,
                      "schedules": model.schedules, "rtc_event_mask": model.event_bits[RTC_EVENT],
                      "expiry_count": model.read32(TIMER + 0x30)})

    return {"capsule_sha256": CAPSULE_SHA256, "hardware_accesses": 0,
            "live_timer_state_identified": False, "live_recovery_established": False,
            "assumptions": [
                "earlier startup stage and unrelated initialization hooks complete",
                "timeout cancellation/insertion are modeled boundaries; no scheduler or timer queue executes",
                "two unlinked expiries and a fixed tick value are supplied",
                "event delivery and work submission return supplied results",
                "this establishes instruction ordering, not physical timer or thread progress",
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

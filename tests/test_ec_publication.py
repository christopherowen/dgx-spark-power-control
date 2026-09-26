# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic evidence only: no sysfs, FF-A endpoint or real captures are read."""
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ec_publication", ROOT / "diagnostics/ec-publication/analyze.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


def fixture(*, progressing=False, budgets="0,0,0,0,0,0", traffic=True):
    def mirror(second):
        return f"{second:02}1001010126"

    def endpoint(second):
        return f"version=0305080000 source_mw={budgets} rtc_bcd={mirror(second)}"

    lines = ["capture_format=ec-publication-v1", "captured_at=2026-01-01T01:10:00+00:00",
             "module_sha256=" + "a" * 64, "capture_part=initial", endpoint(0),
             "capture_part=trace", publication.TRACE_HEADER]
    for i in range(24):
        lines.append(f"{i} start_ns={1_000_000_000 + i * 320_000_000} duration_ns=200000 "
                     f"status=08,08 opcode=12 packet_changed={int(traffic and i > 0)} "
                     f"rtc_bcd={mirror(i // 3 if progressing else 0)}")
    lines.extend(["capture_part=final", endpoint(8 if progressing else 0), publication.FOOTER])
    return "\n".join(lines) + "\n"


class PublicationAnalysisTests(unittest.TestCase):
    def test_static_publication_with_independent_packet_activity(self):
        result = publication.analyze(fixture())
        self.assertEqual(result["assessment"], "zero_budgets_static_time_mailbox_changes")
        self.assertFalse(result["rtc_advancing"])
        self.assertEqual(result["packet_changes"], 23)
        self.assertFalse(result["inner_ec_read_status_available"])

    def test_published_limits_with_advancing_time(self):
        result = publication.analyze(fixture(progressing=True,
            budgets="140000,142000,231000,244000,257000,265000"))
        self.assertEqual(result["assessment"], "budgets_and_time_publication_observed")
        self.assertTrue(result["rtc_advancing"])
        self.assertEqual(result["final"]["source_mw"]["syspl4"], 265000)

    def test_absent_traffic_and_partial_publication_remain_inconclusive(self):
        for text in (fixture(traffic=False), fixture(budgets="0,142000,0,244000,0,0"),
                     fixture(progressing=True)):
            with self.subTest(text=text[:100]):
                self.assertEqual(publication.analyze(text)["assessment"], "inconclusive")

    def test_incomplete_or_incoherent_evidence_is_refused(self):
        good = fixture()
        cases = [good.removesuffix(publication.FOOTER + "\n"),
                 good.replace("boot_unchanged=1", "boot_unchanged=0"),
                 good.replace("version=0305080000", "version=0305090000"),
                 good.replace("source_mw=0,", "source_mw=1000001,"),
                 good.replace("rtc_bcd=001001010126", "rtc_bcd=ffffffffffff"),
                 good.replace("rtc_bcd=001001010126", "rtc_bcd=001001320126"),
                 good.replace("1 start_ns=1320000000", "1 start_ns=1000000000"),
                 good.replace("1 start_ns=1320000000", "2 start_ns=1320000000"),
                 good.replace("packet_changed=0", "packet_changed=1", 1),
                 good + "unexpected extra record\n"]
        for text in cases:
            with self.subTest(text=text[-100:]):
                with self.assertRaises(ValueError):
                    publication.analyze(text)

    def test_time_regression_is_not_progress(self):
        text = fixture(progressing=True).replace("rtc_bcd=071001010126", "rtc_bcd=001001010126")
        result = publication.analyze(text)
        self.assertEqual(result["assessment"], "rtc_regressed_or_incoherent")
        self.assertFalse(result["rtc_advancing"])

    def test_busy_samples_are_reported_without_claiming_transport_ownership(self):
        result = publication.analyze(fixture().replace("status=08,08", "status=09,0a"))
        self.assertEqual(result["busy_status_observations"], 48)


class PublicationReadBoundaryTests(unittest.TestCase):
    def test_inventory_guard_distinguishes_current_and_lowest_supported_versions(self):
        script = (ROOT / "diagnostics/ec-publication/collect.sh").read_text()
        program = re.search(r"\| awk '(.*?)' \|\| fail", script, re.S)[1]
        inventory = """
    Firmware Component Name: FLASH
    Firmware Version: SBP:R:2.155.11
    Lowest Supported Firmware Version: Not Specified
    Firmware Component Name: EC Firmware
    Firmware Version: 3.5.8
    Lowest Supported Firmware Version: 3.5.8
"""
        cases = [(inventory, 0),
                 (inventory.replace("SBP:R:2.155.11", "SBP:R:2.155.12"), 1),
                 (inventory.replace("Firmware Version: 3.5.8", "Firmware Version: 3.5.9", 1), 1),
                 (inventory.replace("Firmware Component Name: FLASH", "Firmware Component Name: OTHER"), 1),
                 (inventory + inventory, 1)]
        for text, expected in cases:
            with self.subTest(text=text):
                result = subprocess.run(["awk", program], input=text, text=True, capture_output=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_actual_c_read_allowlist_and_transport_failure(self):
        source = (ROOT / "diagnostics/ec-publication/dgx_ec_publication.c").read_text()
        start = source.index("static int read_fixed(")
        end = source.index("\nstatic int read_publication(", start)
        shim = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
struct ffa_send_direct_data2 { uint64_t data[14]; };
struct ffa_device;
struct msg_ops { int (*sync_send_receive2)(struct ffa_device *, struct ffa_send_direct_data2 *); };
struct ops { struct msg_ops *msg_ops; };
struct ffa_device { struct ops *ops; };
static unsigned int calls;
static int response;
static u32 observed_address, observed_length;
static void put_unaligned_le32(u32 value, u8 *p) {
    for (unsigned int i=0; i<4; i++) p[i] = value >> (8*i);
}
static u32 read_le32(const u8 *p) {
    return (u32)p[0] | (u32)p[1]<<8 | (u32)p[2]<<16 | (u32)p[3]<<24;
}
static int fake_send(struct ffa_device *dev, struct ffa_send_direct_data2 *msg) {
    (void)dev;
    u8 *raw = (u8 *)msg->data;
    calls++;
    assert(raw[0] == 12);
    observed_address = read_le32(raw+1);
    observed_length = read_le32(raw+5);
    for (unsigned int i=9; i<sizeof(msg->data); i++) assert(raw[i] == 0);
    memset(raw, 0x5a, sizeof(msg->data));
    return response;
}
'''
        main = r'''
int main(void) {
    struct msg_ops msg_ops = { .sync_send_receive2 = fake_send };
    struct ops ops = { .msg_ops = &msg_ops };
    struct ffa_device dev = { .ops = &ops };
    u8 output[64];
    for (u32 addr=0x06000500; addr<=0x06000810; addr++) {
        for (u32 len=0; len<=32; len++) {
            int allowed = (addr == 0x06000760 && len == 5) ||
                (addr == 0x06000714 && len == 24) ||
                (addr == 0x06000788 && len == 6) ||
                (addr == 0x06000800 && len == 8) ||
                (addr == 0x06000504 && len == 1);
            unsigned int before = calls;
            memset(output, 0xa5, sizeof(output));
            assert(read_fixed(&dev, addr, len, output) == (allowed ? 0 : -EINVAL));
            assert(calls == before + allowed);
            if (allowed) assert(observed_address == addr && observed_length == len);
            for (unsigned int i=0; i<sizeof(output); i++)
                assert(output[i] == (allowed && i<len ? 0x5a : 0xa5));
        }
    }
    assert(calls == 5);
    response = -EIO;
    memset(output, 0xa5, sizeof(output));
    assert(read_fixed(&dev, 0x06000714, 24, output) == -EIO);
    for (unsigned int i=0; i<sizeof(output); i++) assert(output[i] == 0xa5);
    assert(read_fixed(&dev, 0xffffffff, 0xffffffff, output) == -EINVAL);
    assert(calls == 6);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="dgx-ec-read-tests-") as directory:
            path = Path(directory)
            (path / "probe.c").write_text(shim + source[start:end] + main)
            subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", str(path / "probe.c"),
                "-o", str(path / "probe")], check=True, capture_output=True, text=True)
            subprocess.run([str(path / "probe")], check=True)


if __name__ == "__main__":
    unittest.main()

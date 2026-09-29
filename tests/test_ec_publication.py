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
CLIENT_SPEC = importlib.util.spec_from_file_location(
    "ec_clients", ROOT / "diagnostics/ec-publication/inspect_clients.py")
clients = importlib.util.module_from_spec(CLIENT_SPEC)
CLIENT_SPEC.loader.exec_module(clients)


def fixture(*, progressing=False, budgets="0,0,0,0,0,0", traffic=True, version=1):
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
    if version == 2:
        lines[0] = "capture_format=ec-publication-v2"
        lines[6] = publication.TRACE_HEADER_V2
        lines.insert(3, "boot_id=11111111-2222-3333-4444-555555555555")
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

    def test_new_captures_require_matching_canary_contract_and_boot_metadata(self):
        good = fixture(version=2)
        result = publication.analyze(good)
        self.assertTrue(result["version_checks_bracket_samples"])
        self.assertEqual(result["sample_duration_ms"], {"median": 0.2, "max": 0.2})
        for text in (good.replace("version_checks=52", "version_checks=51"),
                     good.replace(publication.TRACE_HEADER_V2, publication.TRACE_HEADER),
                     good.replace("boot_id=11111111", "boot_id=bad"),
                     good.replace("+00:00", "")):
            with self.subTest(text=text[:150]):
                with self.assertRaises(ValueError):
                    publication.analyze(text)
        self.assertFalse(publication.analyze(fixture())["version_checks_bracket_samples"])

    def test_comparison_distinguishes_repeated_fault_from_publication_progress(self):
        def later_capture(text):
            text = text.replace("T01:10:00+", "T02:10:00+")
            return re.sub(r"start_ns=(\d+)",
                          lambda m: f"start_ns={int(m[1]) + 3_600_000_000_000}", text)

        before = publication.analyze(fixture(version=2))
        later = later_capture(fixture(version=2))
        result = publication.compare(before, publication.analyze(later))
        self.assertEqual(result["assessment"], "publication_fault_observed_again")
        self.assertEqual(result["capture_start_separation_seconds"], 3600)
        advancing = later_capture(fixture(version=2, progressing=True,
            budgets="140000,142000,231000,244000,257000,265000"))
        result = publication.compare(before, publication.analyze(advancing))
        self.assertEqual(result["assessment"], "publication_progress_observed")
        self.assertTrue(result["final_source_budgets_changed"])
        with self.assertRaises(ValueError):
            publication.compare(before, before)
        with self.assertRaises(ValueError):
            publication.compare(before, publication.analyze(
                fixture(version=2).replace("T01:10:00+", "T02:10:00+")))
        with self.assertRaises(ValueError):
            publication.compare(publication.analyze(fixture()), before)
        with self.assertRaises(ValueError):
            publication.compare(before, publication.analyze(later.replace("boot_id=11111111", "boot_id=99999999")))


class PublicationReadBoundaryTests(unittest.TestCase):
    def test_client_inventory_finds_shared_partition_and_handles_missing_oem(self):
        with tempfile.TemporaryDirectory(prefix="dgx-client-inventory-") as directory:
            root = Path(directory)
            self.assertEqual(clients.inspect(root)["assessment"], "oem_endpoint_missing_or_ambiguous")
            for name, partition, uuid, driver in (
                    ("oem", "8003", clients.OEM_UUID, None),
                    ("fan", "8003", "78b04d80-d21d-4986-8acb-467b60247ac5", "fan-driver"),
                    ("other", "8002", clients.OEM_UUID, "other-driver")):
                device = root / name
                device.mkdir()
                (device / "modalias").write_text(f"arm_ffa:{partition}:{uuid}\n")
                if driver:
                    (device / "driver").symlink_to(f"/nonexistent-test-drivers/{driver}")
            result = clients.inspect(root)
            self.assertEqual(result["bound_clients"], 1)
            self.assertEqual(len(result["clients"]), 2)
            self.assertEqual(result["firmware_requests"], 0)
            self.assertEqual(result["assessment"], "collection_refused_bound_partition_clients")
            (root / "fan/driver").unlink()
            self.assertEqual(clients.inspect(root)["assessment"],
                             "client_check_passed_other_preflight_checks_required")
            (root / "fan/modalias").write_text("arm_ffa:8003:bad-uuid\n")
            with self.assertRaises(ValueError):
                clients.inspect(root)

    def test_collector_refuses_other_uuid_clients_on_the_same_partition(self):
        script = (ROOT / "diagnostics/ec-publication/collect.sh").read_text()
        start = script.index("endpoint=\n")
        end = script.index('\nmodule="$root/', start)
        guard = script[start:end].replace("/sys/bus/arm_ffa/devices/*", '"$1"/*')
        with tempfile.TemporaryDirectory(prefix="dgx-ffa-guard-") as directory:
            root = Path(directory)
            oem, other = root / "oem", root / "other"
            oem.mkdir()
            other.mkdir()
            (oem / "modalias").write_text("arm_ffa:8003:884a63a0-3285-4120-83aa-eec008a0a546\n")
            (other / "modalias").write_text("arm_ffa:8003:another-uuid\n")
            def run():
                return subprocess.run(["bash", "-c", 'set -eu; fail() { exit 1; };\n' + guard,
                                       "guard", directory], capture_output=True).returncode
            self.assertEqual(run(), 0)
            (other / "driver").symlink_to("/nonexistent-test-driver")
            self.assertEqual(run(), 1)  # Even a different UUID, even a dangling link.
            (other / "modalias").write_text("arm_ffa:8002:another-uuid\n")
            self.assertEqual(run(), 0)
            (oem / "driver").symlink_to("/nonexistent-test-driver")
            self.assertEqual(run(), 1)

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
        end = source.index("\nstatic int capture_run(", start)
        shim = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#define dev_err(...) ((void)0)
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
static int payload_mode, payload_step;
static const u8 expected_version[] = {3, 5, 8, 0, 0};
struct publication { u8 version[5], limits[24], rtc[6]; };
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
    if (payload_mode) {
        payload_step++;
        memset(raw, 0, sizeof(msg->data));
        if (payload_mode != 1 && observed_address == 0x06000760 &&
            !(payload_mode == 4 && payload_step == 5))
            memcpy(raw, expected_version, 5);
        if (payload_mode == 2 && payload_step == 2) memset(raw, 0x5a, 24);
        if (observed_address == 0x06000788) {
            const u8 rtc[] = {0x32, 0x31, 0x07, 0x26, 0x09, 0x26};
            memcpy(raw, rtc, 6);
        }
    }
    return response;
}
'''
        main = r'''
int main(void) {
    struct msg_ops msg_ops = { .sync_send_receive2 = fake_send };
    struct ops ops = { .msg_ops = &msg_ops };
    struct ffa_device dev = { .ops = &ops };
    u8 output[112];
    for (u32 addr=0x06000500; addr<=0x06000810; addr++) {
        for (u32 len=0; len<=112; len++) {
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
    struct publication p;
    memset(&p, 0xa5, sizeof(p));
    assert(read_publication(&dev, &p) == -EIO);
    response = 0;
    for (payload_mode=1; payload_mode<=4; payload_mode++) {
        if (payload_mode == 3) continue;
        payload_step = 0;
        unsigned int before = calls;
        assert(read_publication(&dev, &p) == (payload_mode == 2 ? -EAGAIN : -EBADMSG));
        assert(calls == before + (payload_mode == 1 ? 1 : payload_mode == 2 ? 3 : 5));
        for (unsigned int i=0; i<sizeof(p); i++) assert(((u8 *)&p)[i] == 0xa5);
    }
    payload_mode = 3;
    payload_step = 0;
    assert(read_publication(&dev, &p) == 0); /* Genuine zero budgets are valid evidence. */
    assert(memcmp(p.version, expected_version, 5) == 0);
    for (unsigned int i=0; i<24; i++) assert(p.limits[i] == 0);
    const u8 rtc[] = {0x32, 0x31, 0x07, 0x26, 0x09, 0x26};
    assert(memcmp(p.rtc, rtc, 6) == 0);
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

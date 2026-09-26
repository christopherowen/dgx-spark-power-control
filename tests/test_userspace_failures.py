# SPDX-License-Identifier: GPL-2.0-only
"""Command behavior against a temporary hwmon tree; never the host's sysfs."""

import contextlib
import errno
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_power_control_contract import CONTROL, POWER_LABELS

TEMPERATURES = (("tj_max", 41500), ("gpu", 39000), ("dla", 27000))


def make_device(root: Path, index: int = 3, writable: bool = True) -> Path:
    device = root / f"hwmon{index}"
    device.mkdir()
    (device / "name").write_text("dgx_spbm_power\n")
    (device / "prochot").write_text("1\n")
    for channel, label in enumerate(POWER_LABELS, 1):
        (device / f"power{channel}_label").write_text(f"{label}\n")
        (device / f"power{channel}_input").write_text(f"{channel * 1_000_000}\n")
    for channel, nvidia in zip(range(11, 15), (140, 142, 231, 244)):
        cap = device / f"power{channel}_cap"
        cap.write_text(f"{nvidia * 1_000_000}\n")
        cap.chmod(0o644 if writable else 0o444)
        (device / f"power{channel}_cap_max").write_text(f"{nvidia * 1_000_000}\n")
        (device / f"power{channel}_cap_min").write_text("15000000\n")
    for channel, (label, value) in enumerate(TEMPERATURES, 1):
        (device / f"temp{channel}_label").write_text(f"{label}\n")
        (device / f"temp{channel}_input").write_text(f"{value}\n")
    return device


def driver_like_write(prefix: Path, microwatts: int) -> None:
    """The driver reads back the applied limit: NVIDIA's value after zero."""
    value = microwatts or int(CONTROL.attribute(prefix, "cap_max").read_text())
    CONTROL.attribute(prefix, "cap").write_text(f"{value}\n")


def unpublished(name: str):
    original = CONTROL.read_integer

    def read(path: Path):
        if path.name == name:
            return None
        return original(path)
    return read


class UserlandFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_and_duplicate_devices_are_rejected(self):
        with self.assertRaises(RuntimeError):
            CONTROL.find_hwmon_device(self.root)
        make_device(self.root, 3)
        other = self.root / "hwmon0"
        other.mkdir()
        (other / "name").write_text("acpitz\n")
        self.assertEqual(CONTROL.find_hwmon_device(self.root), self.root / "hwmon3")
        make_device(self.root, 4)
        with self.assertRaises(RuntimeError):
            CONTROL.find_hwmon_device(self.root)

    def test_duplicate_labels_are_rejected(self):
        device = make_device(self.root)
        (device / "power15_label").write_text("gpu\n")
        with self.assertRaisesRegex(RuntimeError, "duplicate power label 'gpu'"):
            CONTROL.labelled_channels(device, "power")

    def test_set_limit_writes_microwatts_and_authenticates_readback(self):
        device = make_device(self.root)
        CONTROL.set_limit(device, "syspl1", 180_000_000)
        self.assertEqual((device / "power13_cap").read_text(), "180000000\n")
        with patch.object(CONTROL, "read_integer", return_value=231_000_000):
            with self.assertRaisesRegex(RuntimeError, "syspl1 readback mismatch"):
                CONTROL.set_limit(device, "syspl1", 180_000_000)
        with self.assertRaises(ValueError):
            CONTROL.set_limit(device, "gpu", 1_000_000)

    def test_restore_verifies_nvidia_limits_or_reports_them_unverified(self):
        device = make_device(self.root)
        for channel in range(11, 15):
            (device / f"power{channel}_cap").write_text("100000000\n")
        with patch.object(CONTROL, "write_cap", driver_like_write):
            self.assertEqual(CONTROL.restore(device, CONTROL.LIMITS), [])
            self.assertEqual((device / "power11_cap").read_text(), "140000000\n")
            with patch.object(CONTROL, "read_integer", unpublished("power12_cap_max")):
                self.assertEqual(CONTROL.restore(device, ("pl1", "pl2")), ["pl2"])
        with self.assertRaisesRegex(RuntimeError, "pl1 did not return to NVIDIA"):
            CONTROL.restore(device, ("pl1",))

    def test_status_reports_restriction_unpublished_limits_and_control(self):
        device = make_device(self.root)
        (device / "power11_cap").write_text("100000000\n")
        with patch.object(CONTROL, "read_integer", unpublished("power14_cap_max")):
            report = CONTROL.status(device).splitlines()
        self.assertEqual(
            report[0],
            "system=1.00W package=2.00W gpu=8.00W cpu_p=4.00W cpu_e=5.00W "
            "hottest=41.5C prochot=1 control=available",
        )
        self.assertEqual(
            report[1],
            "pl1 average=11.00W cap=100.00W nvidia=140.00W floor=15.00W restricted",
        )
        self.assertEqual(report[4], "syspl2 average=14.00W cap=244.00W nvidia=unpublished floor=15.00W")
        read_only = make_device(self.root, 5, writable=False)
        self.assertTrue(CONTROL.status(read_only).splitlines()[0].endswith("control=disabled"))

    def test_only_enodata_means_unpublished(self):
        path = self.root / "value"
        with patch.object(Path, "read_text", side_effect=OSError(errno.ENODATA, "No data")):
            self.assertIsNone(CONTROL.read_integer(path))
        with patch.object(Path, "read_text", side_effect=OSError(errno.EIO, "I/O error")):
            with self.assertRaises(OSError):
                CONTROL.read_integer(path)

    def test_diagnose_never_writes_and_distinguishes_published_limits(self):
        device = make_device(self.root)
        with patch.object(CONTROL, "write_cap", side_effect=AssertionError("must not write")):
            report = CONTROL.diagnose(device)
            self.assertEqual(report["assessment"], "limits_match_nvidia")
            self.assertIn("does not establish", report["message"])
            (device / "power11_cap").write_text("100000000\n")
            self.assertEqual(CONTROL.diagnose(device)["assessment"], "applied_below_nvidia")
            (device / "power11_cap").write_text("150000000\n")
            self.assertEqual(CONTROL.diagnose(device)["applied_above_nvidia"], ["pl1"])

    def test_diagnose_reports_full_and_partial_unpublished_state(self):
        device = make_device(self.root)
        for channel, mw in zip(range(11, 15), (20, 20, 30, 30)):
            (device / f"power{channel}_cap").write_text(f"{mw * 1_000_000}\n")
        original = CONTROL.read_integer
        def missing(path):
            return None if path.name.endswith("_cap_max") else original(path)
        with patch.object(CONTROL, "read_integer", side_effect=missing):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = CONTROL.main(["--hwmon-root", str(self.root), "diagnose", "--json"])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(report["assessment"], "nvidia_limits_unpublished")
            self.assertEqual(report["unpublished_limits"], list(CONTROL.LIMITS))
            self.assertTrue(report["matches_observed_fallback"])
            self.assertEqual(report["limits"]["syspl1"]["applied_uw"], 30_000_000)
            self.assertIn("No restart-free", report["message"])
        with patch.object(CONTROL, "read_integer", unpublished("power12_cap_max")):
            report = CONTROL.diagnose(device)
            self.assertEqual(report["unpublished_limits"], ["pl2"])
            self.assertFalse(report["matches_observed_fallback"])

    def test_diagnose_read_failure_cannot_look_healthy(self):
        device = make_device(self.root)
        (device / "power11_cap").write_text("0\n")
        with self.assertRaisesRegex(RuntimeError, "applied limit"):
            CONTROL.diagnose(device)
        (device / "power11_cap").unlink()
        with self.assertLogs("dgx-power-control", "ERROR"):
            self.assertEqual(CONTROL.main(["--hwmon-root", str(self.root), "diagnose"]), 1)

    def test_main_reports_failures_without_traceback(self):
        make_device(self.root)
        root = ["--hwmon-root", str(self.root)]
        with self.assertLogs("dgx-power-control", "ERROR") as logs:
            self.assertEqual(CONTROL.main(root + ["set-limit", "pl1", "99.0005"]), 1)
            self.assertEqual(CONTROL.main(root + ["automatic", "cpu_e"]), 1)
        self.assertIn("three decimal places", logs.output[0])
        self.assertIn("limit must be one of", logs.output[1])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(CONTROL.main(root + ["status"]), 0)
        self.assertIn("control=available", output.getvalue())
        self.assertEqual(CONTROL.main(root + ["set-limit", "pl2", "100"]), 0)
        self.assertEqual((self.root / "hwmon3/power12_cap").read_text(), "100000000\n")


if __name__ == "__main__":
    unittest.main()

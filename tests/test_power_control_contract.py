# SPDX-License-Identifier: GPL-2.0-only
"""Source and packaging contract checks; no hardware or sysfs access."""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kernel" / "dgx_spbm_power_control.c").read_text()
MODULE_PATH = ROOT / "userspace" / "dgx_power_control.py"
SPEC = importlib.util.spec_from_file_location("dgx_power_control", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)

REGISTERS = dict(re.findall(
    r'\[(DGX_SPBM_[A-Z0-9_]+)\]\s*= \{ "([A-Z0-9_]+)", 0x[0-9a-f]+ \}', SOURCE))
OFFSETS = dict(re.findall(r'\[(DGX_SPBM_[A-Z0-9_]+)\]\s*= \{ "[A-Z0-9_]+", (0x[0-9a-f]+) \}', SOURCE))
POWER_LABELS = re.findall(
    r'\{ "([a-z0-9_]+)", DGX_SPBM_[A-Z0-9_]+ \}',
    SOURCE.split("dgx_spbm_power_channels[] = {", 1)[1].split("};", 1)[0])


def function_body(name):
    return re.search(rf"\n[^\n]*\b{name}\([^;{{]*\)\s*\{{.*?\n\}}", SOURCE, re.S)[0]


class KernelContractTests(unittest.TestCase):
    def test_register_table_is_complete_unique_and_in_the_page(self):
        enum = SOURCE.split("enum dgx_spbm_register_id {", 1)[1].split("};", 1)[0]
        ids = re.findall(r"(DGX_SPBM_[A-Z0-9_]+),", enum)
        self.assertEqual(ids[-1], "DGX_SPBM_SYSPL2_HIGH")
        self.assertEqual(set(ids), set(REGISTERS))
        self.assertEqual(len(set(REGISTERS.values())), len(REGISTERS))
        offsets = [int(value, 16) for value in OFFSETS.values()]
        self.assertEqual(len(set(offsets)), len(offsets))
        self.assertTrue(all(offset % 4 == 0 and offset <= 0x1000 - 4 for offset in offsets))

    def test_limit_rows_name_their_own_limit_and_source(self):
        table = SOURCE.split("dgx_spbm_limits[] = {", 1)[1].split("};", 1)[0]
        rows = re.findall(r"\{ ([^}]*) \}", table.replace("\n\t  ", " "))
        self.assertEqual(len(rows), 4)
        self.assertEqual(POWER_LABELS[10:], ["pl1", "pl2", "syspl1", "syspl2"])
        for label, row in zip(POWER_LABELS[10:], rows):
            names = [REGISTERS[item.strip()] for item in row.split(",")]
            prefix = f"SPBM_{label.upper()}_"
            self.assertEqual(names, [
                prefix + "VAL_OS_OFFSET", prefix + "VAL_EC_OFFSET",
                prefix + "VAL_UEFI_OFFSET", prefix + "VAL_OFFSET",
                prefix + "LIMIT_LOW_OFFSET", prefix + "LIMIT_HIGH_OFFSET",
            ])

    def test_only_os_limit_slots_and_update_are_ever_written(self):
        calls = re.compile(r"\biowrite32\((?!\))")
        self.assertEqual(len(calls.findall(SOURCE)), 2)
        body = function_body("dgx_spbm_request_limit")
        self.assertEqual(len(calls.findall(body)), 2)
        self.assertIn("dgx_spbm_limits[limit].os", body)
        self.assertIn("DGX_SPBM_UPDATE", body)
        for forbidden in ("writel", "memcpy_toio", "debugfs", "ioctl", "_CLEAR_",
                          "PID_OUTPUT", "BUDGET", "_KP_", "_KI_", "_KD_", "_TAU_"):
            self.assertNotIn(forbidden, SOURCE)

    def test_writes_are_bounded_by_nvidia_limits_not_firmware_maximum(self):
        body = function_body("dgx_spbm_limit_range")
        self.assertIn("if (!ec)\n\t\treturn -ENODATA;", body)
        self.assertIn("*ceiling = min(ec, high);", body)
        self.assertIn("*ceiling = min(*ceiling, uefi);", body)
        self.assertIn("mw < low || mw > ceiling", function_body("dgx_spbm_set_limit"))

    def test_platform_and_firmware_contract_fail_closed(self):
        for requirement in (
            'DMI_SYS_VENDOR, "NVIDIA"',
            'DMI_PRODUCT_NAME, "NVIDIA_DGX_Spark"',
            'DMI_BOARD_NAME, "P4242"',
            "acpi_check_dsm(",
            "dgx_spbm_validate_resource_name(dev, handle)",
            "dgx_spbm_check_register_map(dev, handle)",
            "res->start != DGX_SPBM_BASE",
            "existing %s OS limit %u mW; power-limit control disabled",
            '{ "NVDA8800" }',
        ):
            self.assertIn(requirement, SOURCE)
        self.assertRegex(SOURCE, r"#define DGX_SPBM_BASE\s+0x1c238000U\n")
        self.assertRegex(SOURCE, r"#define DGX_SPBM_SIZE\s+0x1000U\n")
        self.assertRegex(SOURCE, r"#define DGX_SPBM_RESOURCE_INDEX\s+1U\n")
        self.assertNotIn("MODULE_DEVICE_TABLE(", SOURCE)

    def test_orderly_lifecycle_restores_nvidia_limits(self):
        self.assertIn('dgx_spbm_restore(data, "suspend", false)', SOURCE)
        self.assertIn('dgx_spbm_restore(data, "reboot", true)', SOURCE)
        self.assertIn('dgx_spbm_restore(data, "driver removal", true)', SOURCE)
        self.assertIn("devm_register_reboot_notifier", SOURCE)
        self.assertIn("DGX_SPBM_RESTORE_ATTEMPTS", SOURCE)

    def test_packaging_versions_and_names_agree(self):
        version = re.search(r'#define DGX_SPBM_DRIVER_VERSION\s+"([0-9.]+)"', SOURCE)[1]
        dkms = (ROOT / "dkms.conf").read_text()
        self.assertIn(f'PACKAGE_VERSION="{version}"', dkms)
        self.assertIn('BUILT_MODULE_NAME[0]="dgx_spbm_power_control"', dkms)
        self.assertIn("obj-m += dgx_spbm_power_control.o", (ROOT / "kernel/Makefile").read_text())
        boot = (ROOT / "systemd/dgx_spbm_power_control.conf").read_text().splitlines()
        self.assertEqual(boot[-1], "dgx_spbm_power_control")
        self.assertIn('"dgx_spbm_power"', SOURCE)
        self.assertEqual(CONTROL.HWMON_NAME, "dgx_spbm_power")
        self.assertIn(f'MODULE_VERSION(DGX_SPBM_DRIVER_VERSION)', SOURCE)
        for path in ("README.md", "docs/installation.md", "docs/maintenance.md"):
            text = (ROOT / path).read_text()
            for found in re.findall(r"(?:dgx-spark-power-control-|-v )(\d+\.\d+\.\d+)", text):
                self.assertEqual(found, version, path)


class UserlandContractTests(unittest.TestCase):
    def test_limits_match_driver_labels(self):
        self.assertEqual(CONTROL.LIMITS, tuple(POWER_LABELS[10:]))
        for _, label in CONTROL.SUMMARY:
            self.assertIn(label, POWER_LABELS)

    def test_watts_parse_to_whole_milliwatts(self):
        self.assertEqual(CONTROL.parse_watts("100"), 100_000_000)
        self.assertEqual(CONTROL.parse_watts("99.5"), 99_500_000)
        self.assertEqual(CONTROL.parse_watts("0.001"), 1_000)
        for text in ("0", "-5", "nan", "inf", "1e9", "99.0005", "watts", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                CONTROL.parse_watts(text)


if __name__ == "__main__":
    unittest.main()

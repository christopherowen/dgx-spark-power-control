# SPDX-License-Identifier: GPL-2.0-only
"""Execute the driver's actual C functions against a simulated SPBM page.

Only Linux wrappers and the firmware boundary are replaced here; register
tables, map validation, hwmon callbacks, and limit/restore functions are
extracted verbatim from the driver. The simulated firmware recomputes applied
limits only after an update request, one tick per driver sleep.
"""
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kernel/dgx_spbm_power_control.c").read_text()


def c_function(name):
    start = re.search(rf"static [^;{{}}()]*?\b{name}\([^;{{]*\)\s*\{{", SOURCE).start()
    end = SOURCE.index("{", start) + 1
    depth = 1
    while depth:
        depth += (SOURCE[end] == "{") - (SOURCE[end] == "}")
        end += 1
    return SOURCE[start:end]


def c_block(pattern):
    return re.search(pattern + r".*?\n\};", SOURCE, re.S)[0]


SHIM = r'''
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef unsigned short umode_t;
#define __iomem
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define BIT(n) (1UL << (n))
#define BIT_ULL(n) (1ULL << (n))
#define min(a, b) ((a) < (b) ? (a) : (b))
enum hwmon_sensor_types { hwmon_chip, hwmon_temp, hwmon_in, hwmon_curr, hwmon_power, hwmon_energy };
enum { hwmon_power_input = 1, hwmon_power_cap, hwmon_power_cap_max, hwmon_power_cap_min, hwmon_power_label };
typedef u32 acpi_object_type;
#define ACPI_TYPE_INTEGER 1
#define ACPI_TYPE_STRING 2
#define ACPI_TYPE_PACKAGE 4
union acpi_object {
 acpi_object_type type;
 struct { acpi_object_type type; u64 value; } integer;
 struct { acpi_object_type type; u32 length; char *pointer; } string;
 struct { acpi_object_type type; u32 count; union acpi_object *elements; } package;
};
struct device { void *drvdata; };
static void *dev_get_drvdata(const struct device *dev) { return dev->drvdata; }
struct mutex { int held; };
struct notifier_block { int unused; };
static int mutex_lock_interruptible(struct mutex *m) { assert(!m->held); m->held = 1; return 0; }
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held = 1; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held = 0; }
static char log_text[65536];
static size_t log_length;
static void capture_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void capture_log(const char *fmt, ...) {
 va_list args; va_start(args, fmt);
 int n = vsnprintf(log_text + log_length, sizeof(log_text) - log_length, fmt, args);
 va_end(args); assert(n >= 0 && (size_t)n < sizeof(log_text) - log_length);
 log_length += n;
}
#define dev_info(dev, ...) ((void)(dev), capture_log(__VA_ARGS__))
#define dev_warn(dev, ...) ((void)(dev), capture_log(__VA_ARGS__))
#define dev_warn_ratelimited(dev, ...) ((void)(dev), capture_log(__VA_ARGS__))
#define dev_err(dev, ...) ((void)(dev), capture_log(__VA_ARGS__))
'''

FIRMWARE = r'''
static u32 page[DGX_SPBM_SIZE / 4];
static unsigned int limit_writes, update_requests, ticks;
static int fw_ignore, fw_delay, fw_skew;
static u32 fw_race_pl1;
static const u32 fallback_mw[] = { 20000, 20000, 30000, 30000 };
static size_t page_offset(const void *addr) {
 size_t offset = (size_t)((const u8 *)addr - (const u8 *)page);
 assert(offset < sizeof(page) && !(offset & 3));
 return offset;
}
static u32 reg(enum dgx_spbm_register_id id) { return page[dgx_spbm_registers[id].offset / 4]; }
static void set_reg(enum dgx_spbm_register_id id, u32 value) { page[dgx_spbm_registers[id].offset / 4] = value; }
static u32 ioread32(const void *addr) { return page[page_offset(addr) / 4]; }
static void iowrite32(u32 value, void *addr) {
 size_t offset = page_offset(addr);
 bool os_slot = false;
 for (size_t i = 0; i < ARRAY_SIZE(dgx_spbm_limits); i++)
  os_slot |= offset == dgx_spbm_registers[dgx_spbm_limits[i].os].offset;
 if (offset == dgx_spbm_registers[DGX_SPBM_UPDATE].offset) {
  assert(value == DGX_SPBM_UPDATE_REQUEST);
  update_requests++;
  if (fw_race_pl1) set_reg(DGX_SPBM_PL1_OS, fw_race_pl1);
 } else {
  assert(os_slot); /* The driver may write nothing else. */
  limit_writes++;
 }
 page[offset / 4] = value;
}
/* Independent model: lowest nonzero source applies, else a fixed fallback. */
static void firmware_tick(void) {
 ticks++;
 if (!reg(DGX_SPBM_UPDATE) || fw_ignore) return;
 if (fw_delay > 0) { fw_delay--; return; }
 for (size_t i = 0; i < ARRAY_SIZE(dgx_spbm_limits); i++) {
  const struct dgx_spbm_limit *l = &dgx_spbm_limits[i];
  u32 sources[] = { reg(l->os), reg(l->ec), reg(l->uefi) }, lowest = 0;
  for (size_t s = 0; s < 3; s++)
   if (sources[s] && (!lowest || sources[s] < lowest)) lowest = sources[s];
  set_reg(l->applied, (lowest ? lowest : fallback_mw[i]) + fw_skew);
 }
 set_reg(DGX_SPBM_UPDATE, 0);
}
static void msleep(unsigned int ms) { (void)ms; firmware_tick(); }
'''

MAIN = r'''
static struct dgx_spbm_data data;
static struct device hwmon_dev = { .drvdata = &data };
static const u32 ec_mw[] = { 140000, 142000, 231000, 244000 };

static void healthy(void) {
 memset(page, 0, sizeof(page));
 memset(&data, 0, sizeof(data));
 data.base = page;
 data.control_enabled = true;
 for (size_t i = 0; i < ARRAY_SIZE(dgx_spbm_limits); i++) {
  set_reg(dgx_spbm_limits[i].ec, ec_mw[i]);
  set_reg(dgx_spbm_limits[i].applied, ec_mw[i]);
  set_reg(dgx_spbm_limits[i].low, i < 2 ? 15000 : 25000);
  set_reg(dgx_spbm_limits[i].high, i < 2 ? 250000 : 300000);
 }
 set_reg(DGX_SPBM_SYS_TOTAL, 25000);
}
static int cap(int channel, long microwatts) {
 return dgx_spbm_hwmon_write(&hwmon_dev, hwmon_power, hwmon_power_cap, channel, microwatts);
}
static long read_ok(enum hwmon_sensor_types type, u32 attr, int channel) {
 long value = -1;
 assert(dgx_spbm_hwmon_read(&hwmon_dev, type, attr, channel, &value) == 0);
 return value;
}
static int read_ret(enum hwmon_sensor_types type, u32 attr, int channel) {
 long value = -1;
 return dgx_spbm_hwmon_read(&hwmon_dev, type, attr, channel, &value);
}

/* Firmware-shaped map: an UPDATE group, then pinned names mixed with others. */
enum mutation { NONE, WRONG_OFFSET, MISSING, DUPLICATE, BAD_COUNT, NAME_INTEGER,
                OFFSET_STRING, EVEN_GROUP, GROUP_INTEGER, MAP_INTEGER };
static union acpi_object items[2 * DGX_SPBM_REGISTER_COUNT + 16], groups[2], map;
static void string_object(union acpi_object *object, const char *text) {
 object->string.type = ACPI_TYPE_STRING;
 object->string.length = strlen(text);
 object->string.pointer = (char *)text;
}
static void integer_object(union acpi_object *object, u64 value) {
 object->integer.type = ACPI_TYPE_INTEGER;
 object->integer.value = value;
}
static void build_map(enum mutation mutation, enum dgx_spbm_register_id target) {
 unsigned int n = 1;
 memset(items, 0, sizeof(items));
 integer_object(&items[0], 1);
 string_object(&items[1], "UPDATE_SPBM");
 integer_object(&items[2], 0);
 groups[0].package.type = ACPI_TYPE_PACKAGE;
 groups[0].package.count = 3;
 groups[0].package.elements = &items[0];
 union acpi_object *group = &items[3];
 for (unsigned int id = DGX_SPBM_UPDATE + 1; id < DGX_SPBM_REGISTER_COUNT; id++) {
  if (mutation == MISSING && id == target) continue;
  string_object(&group[n++], dgx_spbm_registers[id].name);
  integer_object(&group[n++], dgx_spbm_registers[id].offset +
                 (mutation == WRONG_OFFSET && id == target ? 4 : 0));
  if (id == DGX_SPBM_PL2_OS) { /* Unused firmware names are ignored. */
   string_object(&group[n++], "SPBM_PL3_VAL_OS_OFFSET");
   integer_object(&group[n++], 0x108);
  }
 }
 if (mutation == DUPLICATE) {
  string_object(&group[n++], dgx_spbm_registers[target].name);
  integer_object(&group[n++], dgx_spbm_registers[target].offset);
 }
 integer_object(&group[0], n / 2 + (mutation == BAD_COUNT));
 if (mutation == NAME_INTEGER) integer_object(&group[1], 7);
 if (mutation == OFFSET_STRING) string_object(&group[2], "0x048");
 if (mutation == EVEN_GROUP) n--;
 groups[1].package.type = ACPI_TYPE_PACKAGE;
 groups[1].package.count = n;
 groups[1].package.elements = group;
 if (mutation == GROUP_INTEGER) integer_object(&groups[1], 1);
 map.package.type = ACPI_TYPE_PACKAGE;
 map.package.count = 2;
 map.package.elements = groups;
 if (mutation == MAP_INTEGER) integer_object(&map, 1);
}
static int validate(enum mutation mutation, enum dgx_spbm_register_id target) {
 struct device dev = { 0 };
 build_map(mutation, target);
 return dgx_spbm_validate_map(&dev, &map);
}

int main(int argc, char **argv) {
 assert(argc == 2);
 const char *c = argv[1];
 healthy();
 if (!strcmp(c, "map_valid")) {
  assert(validate(NONE, DGX_SPBM_UPDATE) == 0 && log_length == 0);
 } else if (!strcmp(c, "map_wrong_offset")) {
  assert(validate(WRONG_OFFSET, DGX_SPBM_PL1_OS) == -EBADMSG);
  assert(strstr(log_text, "register map mismatch: SPBM_PL1_VAL_OS_OFFSET"));
 } else if (!strcmp(c, "map_missing")) {
  assert(validate(MISSING, DGX_SPBM_SYSPL2_HIGH) == -ENOENT);
  assert(strstr(log_text, "register map lacks SPBM_SYSPL2_LIMIT_HIGH_OFFSET"));
 } else if (!strcmp(c, "map_duplicate")) {
  assert(validate(DUPLICATE, DGX_SPBM_UPDATE) == -EBADMSG);
  assert(validate(DUPLICATE, DGX_SPBM_GPU_ENERGY) == -EBADMSG);
 } else if (!strcmp(c, "map_malformed")) {
  for (enum mutation m = BAD_COUNT; m <= MAP_INTEGER; m++)
   assert(validate(m, DGX_SPBM_UPDATE) == -EBADMSG);
 } else if (!strcmp(c, "telemetry_units")) {
  const char *label;
  set_reg(DGX_SPBM_PKG_ENERGY, 123456);
  set_reg(DGX_SPBM_TJ_MAX, 3150);
  set_reg(DGX_SPBM_PL1_AVG, 18000);
  assert(read_ok(hwmon_power, hwmon_power_input, 0) == 25000000);
  assert(read_ok(hwmon_power, hwmon_power_input, 10) == 18000000);
  assert(read_ok(hwmon_energy, 0, 0) == 123456000);
  assert(read_ok(hwmon_temp, 0, 0) == 31500);
  assert(dgx_spbm_hwmon_read_string(&hwmon_dev, hwmon_power, hwmon_power_label, 13, &label) == 0);
  assert(!strcmp(label, "syspl2"));
  assert(dgx_spbm_hwmon_read_string(&hwmon_dev, hwmon_temp, 0, 7, &label) == 0 && !strcmp(label, "dla"));
  assert(dgx_spbm_hwmon_read_string(&hwmon_dev, hwmon_energy, 0, 3, &label) == 0 && !strcmp(label, "gpu"));
  assert(dgx_spbm_hwmon_read_string(&hwmon_dev, hwmon_chip, 0, 0, &label) == -EOPNOTSUPP);
  set_reg(DGX_SPBM_SYS_TOTAL, DGX_SPBM_MAX_PLAUSIBLE_MW + 1);
  set_reg(DGX_SPBM_TJ_MAX, DGX_SPBM_MAX_PLAUSIBLE_CENTI_C + 1);
  assert(read_ret(hwmon_power, hwmon_power_input, 0) == -EBADMSG);
  assert(read_ret(hwmon_temp, 0, 0) == -EBADMSG);
  assert(read_ret(hwmon_chip, 0, 0) == -EOPNOTSUPP);
 } else if (!strcmp(c, "limit_attributes")) {
  assert(read_ok(hwmon_power, hwmon_power_cap, 10) == 140000000);
  assert(read_ok(hwmon_power, hwmon_power_cap_min, 10) == 15000000);
  assert(read_ok(hwmon_power, hwmon_power_cap_max, 10) == 140000000);
  assert(read_ok(hwmon_power, hwmon_power_cap_max, 12) == 231000000);
  set_reg(DGX_SPBM_PL1_UEFI, 120000);
  assert(read_ok(hwmon_power, hwmon_power_cap_max, 10) == 120000000);
  set_reg(DGX_SPBM_PL1_HIGH, 110000);
  assert(read_ok(hwmon_power, hwmon_power_cap_max, 10) == 110000000);
  set_reg(DGX_SPBM_PL1_EC, 0);
  set_reg(DGX_SPBM_PL1_APPLIED, 20000);
  assert(read_ret(hwmon_power, hwmon_power_cap_max, 10) == -ENODATA);
  assert(read_ok(hwmon_power, hwmon_power_cap, 10) == 20000000);
  assert(read_ret(hwmon_power, hwmon_power_cap_max, 0) == -EOPNOTSUPP);
  assert(dgx_spbm_is_visible(&data, hwmon_power, hwmon_power_cap, 10) == 0644);
  assert(dgx_spbm_is_visible(&data, hwmon_power, hwmon_power_cap_max, 10) == 0444);
  data.control_enabled = false;
  assert(dgx_spbm_is_visible(&data, hwmon_power, hwmon_power_cap, 10) == 0444);
 } else if (!strcmp(c, "set_and_restore")) {
  assert(cap(10, 100000000) == 0);
  assert(reg(DGX_SPBM_PL1_OS) == 100000 && reg(DGX_SPBM_PL1_APPLIED) == 100000);
  assert(data.owned_mw[0] == 100000 && !data.unsettled && update_requests == 1);
  assert(read_ok(hwmon_power, hwmon_power_cap, 10) == 100000000);
  assert(strstr(log_text, "pl1 OS limit=100000 mW\n"));
  assert(cap(10, 0) == 0);
  assert(reg(DGX_SPBM_PL1_OS) == 0 && reg(DGX_SPBM_PL1_APPLIED) == 140000);
  assert(data.owned_mw[0] == 0 && update_requests == 2 && limit_writes == 2);
  assert(strstr(log_text, "pl1 OS limit=0 mW (NVIDIA default)"));
  assert(!data.lock.held);
 } else if (!strcmp(c, "unchanged_requests_do_not_write")) {
  assert(cap(12, 200000000) == 0 && cap(12, 200000000) == 0);
  assert(limit_writes == 1 && update_requests == 1);
  assert(cap(13, 0) == 0 && limit_writes == 1 && log_length == strlen("syspl1 OS limit=200000 mW\n"));
 } else if (!strcmp(c, "bounds")) {
  assert(cap(10, 141000000) == -EINVAL);
  assert(cap(10, 250000000) == -EINVAL); /* Firmware maximum, above NVIDIA's. */
  assert(cap(10, 14999000) == -EINVAL);
  assert(limit_writes == 0 && update_requests == 0);
  assert(cap(10, 15000000) == 0 && cap(10, 140000000) == 0);
  set_reg(DGX_SPBM_SYSPL1_UEFI, 200000);
  assert(cap(12, 210000000) == -EINVAL && cap(12, 190000000) == 0);
  assert(reg(DGX_SPBM_SYSPL1_APPLIED) == 190000);
 } else if (!strcmp(c, "units")) {
  assert(cap(10, -1) == -EINVAL && cap(10, 100000500) == -EINVAL && cap(10, 999) == -EINVAL);
  assert(cap(10, (long)(DGX_SPBM_MAX_PLAUSIBLE_MW + 1) * 1000) == -EINVAL);
  assert(cap(0, 10000000) == -EOPNOTSUPP && cap(9, 10000000) == -EOPNOTSUPP);
  assert(dgx_spbm_hwmon_write(&hwmon_dev, hwmon_power, hwmon_power_cap_max, 10, 100000000) == -EOPNOTSUPP);
  assert(dgx_spbm_hwmon_write(&hwmon_dev, hwmon_energy, 0, 0, 0) == -EOPNOTSUPP);
  assert(limit_writes == 0 && update_requests == 0);
 } else if (!strcmp(c, "unpublished_nvidia_limit")) {
  set_reg(DGX_SPBM_PL2_EC, 0); /* The dgx3 signature: EC slots zero, fallback applied. */
  set_reg(DGX_SPBM_PL2_APPLIED, 20000);
  assert(cap(11, 15000000) == -ENODATA && cap(11, 100000000) == -ENODATA);
  assert(cap(11, 0) == 0 && limit_writes == 0 && log_length == 0);
  assert(cap(10, 100000000) == 0); /* Other limits remain usable. */
 } else if (!strcmp(c, "implausible_range")) {
  set_reg(DGX_SPBM_PL1_LOW, 0);
  assert(cap(10, 100000000) == -EBADMSG);
  set_reg(DGX_SPBM_PL1_LOW, 150000);
  assert(cap(10, 100000000) == -EBADMSG);
  set_reg(DGX_SPBM_PL1_LOW, 15000);
  set_reg(DGX_SPBM_PL1_EC, DGX_SPBM_MAX_PLAUSIBLE_MW + 1);
  assert(cap(10, 100000000) == -EBADMSG);
  assert(read_ret(hwmon_power, hwmon_power_cap_max, 10) == -EBADMSG);
  assert(limit_writes == 0);
 } else if (!strcmp(c, "firmware_ignores_request")) {
  fw_ignore = 1;
  assert(cap(10, 100000000) == -ETIMEDOUT);
  assert(reg(DGX_SPBM_PL1_OS) == 0 && data.owned_mw[0] == 0 && !data.unsettled);
  assert(limit_writes == 2 && update_requests == 2 && ticks == DGX_SPBM_SETTLE_ATTEMPTS);
  assert(strstr(log_text, "pl1 applied limit 140000 mW did not reach 100000 mW"));
 } else if (!strcmp(c, "firmware_disagrees")) {
  fw_skew = 1000;
  assert(cap(10, 100000000) == -ETIMEDOUT);
  assert(strstr(log_text, "limit change failed (-"));
  assert(reg(DGX_SPBM_PL1_OS) == 0 && data.owned_mw[0] == 0 && data.unsettled == BIT(0));
  assert(dgx_spbm_restore(&data, "test", false) == -ETIMEDOUT);
  assert(limit_writes == 2 && strstr(log_text, "failed to restore NVIDIA power limits for test"));
  assert(data.control_enabled && data.unsettled == BIT(0));
 } else if (!strcmp(c, "slow_firmware_restore_retry")) {
  assert(cap(10, 100000000) == 0);
  fw_delay = DGX_SPBM_SETTLE_ATTEMPTS + 5;
  assert(dgx_spbm_restore(&data, "driver removal", true) == 0);
  assert(reg(DGX_SPBM_PL1_OS) == 0 && reg(DGX_SPBM_PL1_APPLIED) == 140000 && !data.unsettled);
  assert(limit_writes == 2 && update_requests == 2);
  assert(strstr(log_text, "restored 1 NVIDIA default power limit(s) for driver removal"));
 } else if (!strcmp(c, "final_restore_fences_writes")) {
  assert(cap(10, 100000000) == 0 && cap(12, 200000000) == 0);
  assert(dgx_spbm_restore(&data, "driver removal", true) == 0);
  assert(reg(DGX_SPBM_PL1_OS) == 0 && reg(DGX_SPBM_SYSPL1_OS) == 0);
  assert(reg(DGX_SPBM_SYSPL1_APPLIED) == 231000);
  assert(strstr(log_text, "restored 2 NVIDIA default power limit(s) for driver removal"));
  assert(!data.control_enabled && cap(10, 100000000) == -EPERM && limit_writes == 4);
 } else if (!strcmp(c, "suspend_restore_keeps_control")) {
  assert(cap(11, 100000000) == 0);
  assert(dgx_spbm_restore(&data, "suspend", false) == 0);
  assert(reg(DGX_SPBM_PL2_OS) == 0 && data.control_enabled);
  assert(cap(11, 100000000) == 0);
 } else if (!strcmp(c, "restore_without_ownership")) {
  assert(dgx_spbm_restore(&data, "reboot", true) == 0);
  assert(limit_writes == 0 && update_requests == 0);
  assert(strstr(log_text, "no driver-owned power limits to restore for reboot"));
 } else if (!strcmp(c, "foreign_writer_before_set")) {
  set_reg(DGX_SPBM_PL2_OS, 90000);
  assert(cap(11, 100000000) == -ESTALE && cap(11, 0) == -ESTALE);
  assert(limit_writes == 0 && reg(DGX_SPBM_PL2_OS) == 90000);
  assert(strstr(log_text, "pl2 OS limit ownership mismatch: observed=90000 owned=0 mW"));
 } else if (!strcmp(c, "foreign_writer_after_set")) {
  assert(cap(10, 100000000) == 0 && cap(12, 200000000) == 0);
  set_reg(DGX_SPBM_PL1_OS, 90000);
  assert(dgx_spbm_restore(&data, "driver removal", true) == -ESTALE);
  assert(reg(DGX_SPBM_PL1_OS) == 90000 && reg(DGX_SPBM_SYSPL1_OS) == 0);
  assert(limit_writes == 3 && strstr(log_text, "failed to restore NVIDIA power limits for driver removal"));
 } else if (!strcmp(c, "foreign_slot_does_not_block_retry")) {
  assert(cap(10, 100000000) == 0 && cap(12, 200000000) == 0);
  set_reg(DGX_SPBM_PL1_OS, 90000);
  fw_delay = DGX_SPBM_SETTLE_ATTEMPTS + 5;
  assert(dgx_spbm_restore(&data, "driver removal", true) == -ESTALE);
  assert(reg(DGX_SPBM_SYSPL1_OS) == 0 && reg(DGX_SPBM_SYSPL1_APPLIED) == 231000);
  assert(!data.unsettled && reg(DGX_SPBM_PL1_OS) == 90000 && limit_writes == 3);
 } else if (!strcmp(c, "racing_writer_during_set")) {
  fw_race_pl1 = 95000;
  assert(cap(10, 100000000) == -EIO);
  fw_race_pl1 = 0;
  assert(reg(DGX_SPBM_PL1_OS) == 95000 && limit_writes == 1);
  assert(cap(10, 0) == -ESTALE && dgx_spbm_restore(&data, "reboot", true) == -ESTALE);
  assert(reg(DGX_SPBM_PL1_OS) == 95000 && limit_writes == 1);
 } else if (!strcmp(c, "unpublished_after_set")) {
  assert(cap(10, 100000000) == 0);
  set_reg(DGX_SPBM_PL1_EC, 0); /* Limits vanish while an override is active. */
  assert(dgx_spbm_restore(&data, "driver removal", true) == 0);
  assert(reg(DGX_SPBM_PL1_OS) == 0 && reg(DGX_SPBM_UPDATE) == DGX_SPBM_UPDATE_REQUEST);
  assert(strstr(log_text, "pl1 OS limit cleared; no published limit to verify"));
  firmware_tick();
  assert(reg(DGX_SPBM_PL1_APPLIED) == 20000); /* Firmware fallback, not verified. */
 } else {
  assert(!"unknown case");
 }
 assert(!data.lock.held);
 return 0;
}
'''

CASES = tuple(re.findall(r'!strcmp\(c, "([a-z_]+)"\)', MAIN))


class KernelLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="dgx-spbm-c-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        pieces = [
            SHIM,
            "\n".join(re.findall(r"^#define DGX_SPBM_.*$", SOURCE, re.M)),
            c_block(r"enum dgx_spbm_register_id \{"),
            c_block(r"struct dgx_spbm_register \{"),
            c_block(r"static const struct dgx_spbm_register dgx_spbm_registers\["),
            c_block(r"struct dgx_spbm_channel \{"),
            c_block(r"static const struct dgx_spbm_channel dgx_spbm_power_channels\["),
            c_block(r"static const struct dgx_spbm_channel dgx_spbm_energy_channels\["),
            c_block(r"static const struct dgx_spbm_channel dgx_spbm_temp_channels\["),
            c_block(r"struct dgx_spbm_limit \{"),
            c_block(r"static const struct dgx_spbm_limit dgx_spbm_limits\["),
            c_block(r"struct dgx_spbm_data \{"),
            FIRMWARE,
        ]
        names = (
            "dgx_spbm_limit_label", "dgx_spbm_read", "dgx_spbm_request_limit",
            "dgx_spbm_expected_mw", "dgx_spbm_limit_range", "dgx_spbm_settle_locked",
            "dgx_spbm_write_limit_locked", "dgx_spbm_set_limit",
            "dgx_spbm_restore_locked", "dgx_spbm_restore", "dgx_spbm_read_mw",
            "dgx_spbm_read_power", "dgx_spbm_hwmon_read", "dgx_spbm_hwmon_read_string",
            "dgx_spbm_hwmon_write", "dgx_spbm_is_visible", "dgx_spbm_name_is",
            "dgx_spbm_validate_map",
        )
        pieces += [c_function(name) for name in names] + [MAIN]
        source = root / "limits.c"
        source.write_text("\n".join(pieces))
        cls.binary = root / "limits"
        # Kbuild does not enable -Wunused-parameter; hwmon callbacks rely on that.
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
            "-std=gnu11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror",
            str(source), "-o", str(cls.binary),
        ], check=True, capture_output=True, text=True)

    def test_every_scenario_passes(self):
        self.assertGreaterEqual(len(CASES), 20)
        for case in CASES:
            with self.subTest(case=case):
                result = subprocess.run([str(self.binary), case], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_scenario_fails(self):
        result = subprocess.run([str(self.binary), "no_such_case"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

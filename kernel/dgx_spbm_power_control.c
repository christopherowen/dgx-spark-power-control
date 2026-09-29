// SPDX-License-Identifier: GPL-2.0-only
/*
 * NVIDIA DGX Spark SPBM power telemetry and restrictive power limits.
 *
 * The SoC's System Power Budget Manager (SPBM) firmware publishes power,
 * energy, and temperature telemetry in a 4 KiB page described by the MTEL
 * (NVDA8800) ACPI device.  This driver maps only that page, and every register
 * it uses must match the firmware's own _DSM register map.  NVIDIA's published
 * limits remain the ceiling: the four OS limit slots can only lower PL1, PL2,
 * SysPL1, and SysPL2.  Writing zero, removal, suspend, and reboot restore the
 * firmware default.  There is no raw-register, PID, budget, or counter-clear
 * interface.
 *
 * Channel selection and labels follow spark_hwmon by Antheas Kapenekakis.
 */

#include <linux/acpi.h>
#include <linux/bits.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/dmi.h>
#include <linux/err.h>
#include <linux/errno.h>
#include <linux/hwmon.h>
#include <linux/hwmon-sysfs.h>
#include <linux/io.h>
#include <linux/kernel.h>
#include <linux/minmax.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/pm.h>
#include <linux/reboot.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/types.h>
#include <linux/uuid.h>

#define DGX_SPBM_BASE			0x1c238000U
#define DGX_SPBM_SIZE			0x1000U
#define DGX_SPBM_RESOURCE_INDEX		1U
#define DGX_SPBM_DSM_REVISION		0U
#define DGX_SPBM_DSM_RESOURCES		1U
#define DGX_SPBM_DSM_REGISTER_MAP	2U
#define DGX_SPBM_UPDATE_REQUEST		1U
#define DGX_SPBM_MAX_PLAUSIBLE_MW	1000000U
#define DGX_SPBM_MAX_PLAUSIBLE_CENTI_C	20000U
#define DGX_SPBM_SETTLE_POLL_MS		20U
#define DGX_SPBM_SETTLE_ATTEMPTS	50U
#define DGX_SPBM_RESTORE_ATTEMPTS	3U
#define DGX_SPBM_RESTORE_RETRY_MS	100U
#define DGX_SPBM_FIRST_LIMIT_CHANNEL	10
#define DGX_SPBM_DRIVER_VERSION		"0.1.0"

static bool read_only;
module_param(read_only, bool, 0400);
MODULE_PARM_DESC(read_only, "Expose telemetry with all power-limit writes disabled");

static const guid_t dgx_spbm_dsm_guid =
	GUID_INIT(0x12345678, 0x1234, 0x1234,
		  0x12, 0x34, 0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc);

enum dgx_spbm_register_id {
	DGX_SPBM_UPDATE,
	DGX_SPBM_PL_LEVEL,
	DGX_SPBM_PROCHOT,
	DGX_SPBM_TJ_MAX_C,
	DGX_SPBM_SYS_TOTAL,
	DGX_SPBM_SOC_PKG,
	DGX_SPBM_CPU_GPU,
	DGX_SPBM_CPU_P,
	DGX_SPBM_CPU_E,
	DGX_SPBM_VCORE,
	DGX_SPBM_DC_INPUT,
	DGX_SPBM_GPU,
	DGX_SPBM_PREREG,
	DGX_SPBM_DLA,
	DGX_SPBM_PL1_AVG,
	DGX_SPBM_PL2_AVG,
	DGX_SPBM_SYSPL1_AVG,
	DGX_SPBM_SYSPL2_AVG,
	DGX_SPBM_PKG_ENERGY,
	DGX_SPBM_CPU_E_ENERGY,
	DGX_SPBM_CPU_P_ENERGY,
	DGX_SPBM_GPU_ENERGY,
	DGX_SPBM_TJ_MAX,
	DGX_SPBM_CPU_E_CLU0_TEMP,
	DGX_SPBM_CPU_P_CLU0_TEMP,
	DGX_SPBM_CPU_E_CLU1_TEMP,
	DGX_SPBM_CPU_P_CLU1_TEMP,
	DGX_SPBM_GPU_TEMP,
	DGX_SPBM_SOC_TEMP,
	DGX_SPBM_DLA_TEMP,
	DGX_SPBM_PL1_OS,
	DGX_SPBM_PL2_OS,
	DGX_SPBM_SYSPL1_OS,
	DGX_SPBM_SYSPL2_OS,
	DGX_SPBM_PL1_EC,
	DGX_SPBM_PL2_EC,
	DGX_SPBM_SYSPL1_EC,
	DGX_SPBM_SYSPL2_EC,
	DGX_SPBM_PL1_UEFI,
	DGX_SPBM_PL2_UEFI,
	DGX_SPBM_SYSPL1_UEFI,
	DGX_SPBM_SYSPL2_UEFI,
	DGX_SPBM_PL1_APPLIED,
	DGX_SPBM_PL2_APPLIED,
	DGX_SPBM_SYSPL1_APPLIED,
	DGX_SPBM_SYSPL2_APPLIED,
	DGX_SPBM_PL1_LOW,
	DGX_SPBM_PL2_LOW,
	DGX_SPBM_SYSPL1_LOW,
	DGX_SPBM_SYSPL2_LOW,
	DGX_SPBM_PL1_HIGH,
	DGX_SPBM_PL2_HIGH,
	DGX_SPBM_SYSPL1_HIGH,
	DGX_SPBM_SYSPL2_HIGH,
	DGX_SPBM_REGISTER_COUNT
};

struct dgx_spbm_register {
	const char *name;
	u16 offset;
};

/* Observed with SoC firmware 2.155.11.  Probe requires an exact _DSM match. */
static const struct dgx_spbm_register dgx_spbm_registers[DGX_SPBM_REGISTER_COUNT] = {
	[DGX_SPBM_UPDATE]		= { "UPDATE_SPBM", 0x000 },
	[DGX_SPBM_PL_LEVEL]		= { "SPBM_PL_CUR_LEVEL_STATUS_OFFSET", 0x048 },
	[DGX_SPBM_PROCHOT]		= { "SPBM_PROCHOT_STATUS_OFFSET", 0x04c },
	[DGX_SPBM_TJ_MAX_C]		= { "SPBM_PKG_TJ_MAX_C_OFFSET", 0x81c },
	[DGX_SPBM_SYS_TOTAL]		= { "SPBM_TE_SYS_TOTAL_TELEMETRY_OFFSET", 0x300 },
	[DGX_SPBM_SOC_PKG]		= { "SPBM_TE_SOC_PKG_TELEMETRY_OFFSET", 0x304 },
	[DGX_SPBM_CPU_GPU]		= { "SPBM_TE_C_AND_G_TELEMETRY_OFFSET", 0x308 },
	[DGX_SPBM_CPU_P]		= { "SPBM_TE_CPU_P_TELEMETRY_OFFSET", 0x30c },
	[DGX_SPBM_CPU_E]		= { "SPBM_TE_CPU_E_TELEMETRY_OFFSET", 0x310 },
	[DGX_SPBM_VCORE]		= { "SPBM_TE_VCORE_TELEMETRY_OFFSET", 0x314 },
	[DGX_SPBM_DC_INPUT]		= { "SPBM_TE_CHR_TELEMETRY_OFFSET", 0x31c },
	[DGX_SPBM_GPU]			= { "SPBM_TE_TOTAL_GPU_OUT_OFFSET", 0x324 },
	[DGX_SPBM_PREREG]		= { "SPBM_TE_PREREG_IN_OFFSET", 0x338 },
	[DGX_SPBM_DLA]			= { "SPBM_TE_DLA_IN_OFFSET", 0x334 },
	[DGX_SPBM_PL1_AVG]		= { "SPBM_PWR_AVG_EWMA_S_PL1_OFFSET", 0x800 },
	[DGX_SPBM_PL2_AVG]		= { "SPBM_PWR_AVG_EWMA_S_PL2_OFFSET", 0x804 },
	[DGX_SPBM_SYSPL1_AVG]		= { "SPBM_PWR_AVG_EWMA_S_SYSPL1_OFFSET", 0x80c },
	[DGX_SPBM_SYSPL2_AVG]		= { "SPBM_PWR_AVG_EWMA_S_SYSPL2_OFFSET", 0x810 },
	[DGX_SPBM_PKG_ENERGY]		= { "SPBM_PKG_ENERGY_VALUE_ACCUMULATE_OFFSET", 0x344 },
	[DGX_SPBM_CPU_E_ENERGY]		= { "SPBM_CPU_E_ENERGY_VALUE_ACCUMULATE_OFFSET", 0x350 },
	[DGX_SPBM_CPU_P_ENERGY]		= { "SPBM_CPU_P_ENERGY_VALUE_ACCUMULATE_OFFSET", 0x35c },
	[DGX_SPBM_GPU_ENERGY]		= { "SPBM_GPM_ENERGY_VALUE_ACCUMULATE_OFFSET", 0x374 },
	[DGX_SPBM_TJ_MAX]		= { "SPBM_PKG_TJ_MAX_OFFSET", 0x818 },
	[DGX_SPBM_CPU_E_CLU0_TEMP]	= { "SPBM_PKG_THERMAL_ZONE_TEMP_CPU_E_CLU_0_OFFSET", 0x820 },
	[DGX_SPBM_CPU_P_CLU0_TEMP]	= { "SPBM_PKG_THERMAL_ZONE_TEMP_CPU_P_CLU_0_OFFSET", 0x824 },
	[DGX_SPBM_CPU_E_CLU1_TEMP]	= { "SPBM_PKG_THERMAL_ZONE_TEMP_CPU_E_CLU_1_OFFSET", 0x828 },
	[DGX_SPBM_CPU_P_CLU1_TEMP]	= { "SPBM_PKG_THERMAL_ZONE_TEMP_CPU_P_CLU_1_OFFSET", 0x82c },
	[DGX_SPBM_GPU_TEMP]		= { "SPBM_PKG_THERMAL_ZONE_TEMP_GPU_OFFSET", 0x830 },
	[DGX_SPBM_SOC_TEMP]		= { "SPBM_PKG_THERMAL_ZONE_TEMP_SOC_OFFSET", 0x834 },
	[DGX_SPBM_DLA_TEMP]		= { "SPBM_PKG_THERMAL_ZONE_TEMP_DLA_OFFSET", 0x838 },
	[DGX_SPBM_PL1_OS]		= { "SPBM_PL1_VAL_OS_OFFSET", 0x100 },
	[DGX_SPBM_PL2_OS]		= { "SPBM_PL2_VAL_OS_OFFSET", 0x104 },
	[DGX_SPBM_SYSPL1_OS]		= { "SPBM_SYSPL1_VAL_OS_OFFSET", 0x110 },
	[DGX_SPBM_SYSPL2_OS]		= { "SPBM_SYSPL2_VAL_OS_OFFSET", 0x114 },
	[DGX_SPBM_PL1_EC]		= { "SPBM_PL1_VAL_EC_OFFSET", 0x120 },
	[DGX_SPBM_PL2_EC]		= { "SPBM_PL2_VAL_EC_OFFSET", 0x124 },
	[DGX_SPBM_SYSPL1_EC]		= { "SPBM_SYSPL1_VAL_EC_OFFSET", 0x130 },
	[DGX_SPBM_SYSPL2_EC]		= { "SPBM_SYSPL2_VAL_EC_OFFSET", 0x134 },
	[DGX_SPBM_PL1_UEFI]		= { "SPBM_PL1_VAL_UEFI_OFFSET", 0x140 },
	[DGX_SPBM_PL2_UEFI]		= { "SPBM_PL2_VAL_UEFI_OFFSET", 0x144 },
	[DGX_SPBM_SYSPL1_UEFI]		= { "SPBM_SYSPL1_VAL_UEFI_OFFSET", 0x150 },
	[DGX_SPBM_SYSPL2_UEFI]		= { "SPBM_SYSPL2_VAL_UEFI_OFFSET", 0x154 },
	[DGX_SPBM_PL1_APPLIED]		= { "SPBM_PL1_VAL_OFFSET", 0x160 },
	[DGX_SPBM_PL2_APPLIED]		= { "SPBM_PL2_VAL_OFFSET", 0x164 },
	[DGX_SPBM_SYSPL1_APPLIED]	= { "SPBM_SYSPL1_VAL_OFFSET", 0x170 },
	[DGX_SPBM_SYSPL2_APPLIED]	= { "SPBM_SYSPL2_VAL_OFFSET", 0x174 },
	[DGX_SPBM_PL1_LOW]		= { "SPBM_PL1_LIMIT_LOW_OFFSET", 0x708 },
	[DGX_SPBM_PL2_LOW]		= { "SPBM_PL2_LIMIT_LOW_OFFSET", 0x718 },
	[DGX_SPBM_SYSPL1_LOW]		= { "SPBM_SYSPL1_LIMIT_LOW_OFFSET", 0x738 },
	[DGX_SPBM_SYSPL2_LOW]		= { "SPBM_SYSPL2_LIMIT_LOW_OFFSET", 0x748 },
	[DGX_SPBM_PL1_HIGH]		= { "SPBM_PL1_LIMIT_HIGH_OFFSET", 0x70c },
	[DGX_SPBM_PL2_HIGH]		= { "SPBM_PL2_LIMIT_HIGH_OFFSET", 0x71c },
	[DGX_SPBM_SYSPL1_HIGH]		= { "SPBM_SYSPL1_LIMIT_HIGH_OFFSET", 0x73c },
	[DGX_SPBM_SYSPL2_HIGH]		= { "SPBM_SYSPL2_LIMIT_HIGH_OFFSET", 0x74c },
};

struct dgx_spbm_channel {
	const char *label;
	enum dgx_spbm_register_id id;
};

/* Milliwatts.  The last four are the limit controllers' averaged input. */
static const struct dgx_spbm_channel dgx_spbm_power_channels[] = {
	{ "sys_total", DGX_SPBM_SYS_TOTAL },
	{ "soc_pkg", DGX_SPBM_SOC_PKG },
	{ "cpu_gpu", DGX_SPBM_CPU_GPU },
	{ "cpu_p", DGX_SPBM_CPU_P },
	{ "cpu_e", DGX_SPBM_CPU_E },
	{ "vcore", DGX_SPBM_VCORE },
	{ "dc_input", DGX_SPBM_DC_INPUT },
	{ "gpu", DGX_SPBM_GPU },
	{ "prereg", DGX_SPBM_PREREG },
	{ "dla", DGX_SPBM_DLA },
	{ "pl1", DGX_SPBM_PL1_AVG },
	{ "pl2", DGX_SPBM_PL2_AVG },
	{ "syspl1", DGX_SPBM_SYSPL1_AVG },
	{ "syspl2", DGX_SPBM_SYSPL2_AVG },
};

/* Cumulative millijoules. */
static const struct dgx_spbm_channel dgx_spbm_energy_channels[] = {
	{ "pkg", DGX_SPBM_PKG_ENERGY },
	{ "cpu_e", DGX_SPBM_CPU_E_ENERGY },
	{ "cpu_p", DGX_SPBM_CPU_P_ENERGY },
	{ "gpu", DGX_SPBM_GPU_ENERGY },
};

/* Centidegrees Celsius. */
static const struct dgx_spbm_channel dgx_spbm_temp_channels[] = {
	{ "tj_max", DGX_SPBM_TJ_MAX },
	{ "cpu_e_clu0", DGX_SPBM_CPU_E_CLU0_TEMP },
	{ "cpu_p_clu0", DGX_SPBM_CPU_P_CLU0_TEMP },
	{ "cpu_e_clu1", DGX_SPBM_CPU_E_CLU1_TEMP },
	{ "cpu_p_clu1", DGX_SPBM_CPU_P_CLU1_TEMP },
	{ "gpu", DGX_SPBM_GPU_TEMP },
	{ "soc", DGX_SPBM_SOC_TEMP },
	{ "dla", DGX_SPBM_DLA_TEMP },
};

/* Per-source requests, the firmware's applied result, and its fixed range. */
struct dgx_spbm_limit {
	enum dgx_spbm_register_id os, ec, uefi, applied, low, high;
};

/* Indexed like power channels DGX_SPBM_FIRST_LIMIT_CHANNEL onward. */
static const struct dgx_spbm_limit dgx_spbm_limits[] = {
	{ DGX_SPBM_PL1_OS, DGX_SPBM_PL1_EC, DGX_SPBM_PL1_UEFI,
	  DGX_SPBM_PL1_APPLIED, DGX_SPBM_PL1_LOW, DGX_SPBM_PL1_HIGH },
	{ DGX_SPBM_PL2_OS, DGX_SPBM_PL2_EC, DGX_SPBM_PL2_UEFI,
	  DGX_SPBM_PL2_APPLIED, DGX_SPBM_PL2_LOW, DGX_SPBM_PL2_HIGH },
	{ DGX_SPBM_SYSPL1_OS, DGX_SPBM_SYSPL1_EC, DGX_SPBM_SYSPL1_UEFI,
	  DGX_SPBM_SYSPL1_APPLIED, DGX_SPBM_SYSPL1_LOW, DGX_SPBM_SYSPL1_HIGH },
	{ DGX_SPBM_SYSPL2_OS, DGX_SPBM_SYSPL2_EC, DGX_SPBM_SYSPL2_UEFI,
	  DGX_SPBM_SYSPL2_APPLIED, DGX_SPBM_SYSPL2_LOW, DGX_SPBM_SYSPL2_HIGH },
};

struct dgx_spbm_data {
	struct device *dev;
	void __iomem *base;
	struct notifier_block reboot_notifier;
	struct mutex lock; /* Serializes limit writes, restoration, and ownership. */
	u32 owned_mw[ARRAY_SIZE(dgx_spbm_limits)];
	unsigned long unsettled; /* Limits whose applied value is unverified. */
	bool control_enabled;
};

static bool dgx_spbm_is_supported_platform(void)
{
	return dmi_match(DMI_SYS_VENDOR, "NVIDIA") &&
	       dmi_match(DMI_PRODUCT_NAME, "NVIDIA_DGX_Spark") &&
	       dmi_match(DMI_BOARD_NAME, "P4242");
}

static const char *dgx_spbm_limit_label(unsigned int limit)
{
	return dgx_spbm_power_channels[DGX_SPBM_FIRST_LIMIT_CHANNEL + limit].label;
}

static u32 dgx_spbm_read(const struct dgx_spbm_data *data,
			 enum dgx_spbm_register_id id)
{
	return ioread32(data->base + dgx_spbm_registers[id].offset);
}

/* The only device writes: one OS limit slot, then the firmware's update
 * request.  iowrite32() orders the slot before the request.
 */
static void dgx_spbm_request_limit(struct dgx_spbm_data *data,
				   unsigned int limit, u32 mw)
{
	iowrite32(mw, data->base +
		  dgx_spbm_registers[dgx_spbm_limits[limit].os].offset);
	iowrite32(DGX_SPBM_UPDATE_REQUEST,
		  data->base + dgx_spbm_registers[DGX_SPBM_UPDATE].offset);
}

/* Assumed arbitration, consistent with the observed EC-only case: the lowest
 * nonzero source applies.  Zero means no source is published; the firmware's
 * fallback is not modeled.  A wrong assumption fails verification, not open.
 */
static u32 dgx_spbm_expected_mw(u32 os, u32 ec, u32 uefi)
{
	u32 expected = os;

	if (ec && (!expected || ec < expected))
		expected = ec;
	if (uefi && (!expected || uefi < expected))
		expected = uefi;
	return expected;
}

/* NVIDIA's lowest published limit is the ceiling.  EC publication is required:
 * a zero EC slot is the signature of a failed firmware limit transfer.
 */
static int dgx_spbm_limit_range(const struct dgx_spbm_data *data,
				unsigned int limit, u32 *low, u32 *ceiling)
{
	const struct dgx_spbm_limit *l = &dgx_spbm_limits[limit];
	u32 ec = dgx_spbm_read(data, l->ec);
	u32 uefi = dgx_spbm_read(data, l->uefi);
	u32 high = dgx_spbm_read(data, l->high);

	*low = dgx_spbm_read(data, l->low);
	if (!ec)
		return -ENODATA;
	if (ec > DGX_SPBM_MAX_PLAUSIBLE_MW || uefi > DGX_SPBM_MAX_PLAUSIBLE_MW ||
	    high > DGX_SPBM_MAX_PLAUSIBLE_MW || *low > DGX_SPBM_MAX_PLAUSIBLE_MW)
		return -EBADMSG;
	*ceiling = min(ec, high);
	if (uefi)
		*ceiling = min(*ceiling, uefi);
	return 0;
}

static int dgx_spbm_settle_locked(struct dgx_spbm_data *data,
				  unsigned int limit)
{
	const struct dgx_spbm_limit *l = &dgx_spbm_limits[limit];
	u32 ec = dgx_spbm_read(data, l->ec);
	u32 uefi = dgx_spbm_read(data, l->uefi);
	unsigned int attempt;
	u32 expected, applied = 0;

	if (ec > DGX_SPBM_MAX_PLAUSIBLE_MW || uefi > DGX_SPBM_MAX_PLAUSIBLE_MW)
		return -EBADMSG;
	expected = dgx_spbm_expected_mw(data->owned_mw[limit], ec, uefi);
	if (!expected) {
		dev_warn(data->dev,
			 "%s OS limit cleared; no published limit to verify\n",
			 dgx_spbm_limit_label(limit));
		data->unsettled &= ~BIT(limit);
		return 0;
	}

	for (attempt = 0; attempt < DGX_SPBM_SETTLE_ATTEMPTS; attempt++) {
		applied = dgx_spbm_read(data, l->applied);
		if (applied == expected) {
			data->unsettled &= ~BIT(limit);
			return 0;
		}
		msleep(DGX_SPBM_SETTLE_POLL_MS);
	}

	dev_warn_ratelimited(data->dev,
			     "%s applied limit %u mW did not reach %u mW\n",
			     dgx_spbm_limit_label(limit), applied, expected);
	return -ETIMEDOUT;
}

static int dgx_spbm_write_limit_locked(struct dgx_spbm_data *data,
				       unsigned int limit, u32 mw)
{
	/* Record the attempt first: ownership follows what was written. */
	data->unsettled |= BIT(limit);
	data->owned_mw[limit] = mw;
	dgx_spbm_request_limit(data, limit, mw);
	if (dgx_spbm_read(data, dgx_spbm_limits[limit].os) != mw)
		return -EIO;
	return dgx_spbm_settle_locked(data, limit);
}

static int dgx_spbm_set_limit(struct dgx_spbm_data *data, unsigned int limit,
			      u32 mw)
{
	const char *label = dgx_spbm_limit_label(limit);
	u32 os, low, ceiling;
	int restore_ret;
	int ret;

	ret = mutex_lock_interruptible(&data->lock);
	if (ret)
		return ret;
	if (!data->control_enabled) {
		ret = -EPERM;
		goto unlock;
	}

	os = dgx_spbm_read(data, dgx_spbm_limits[limit].os);
	if (os != data->owned_mw[limit]) {
		dev_warn_ratelimited(data->dev,
				     "%s OS limit ownership mismatch: observed=%u owned=%u mW\n",
				     label, os, data->owned_mw[limit]);
		ret = -ESTALE;
		goto unlock;
	}

	if (mw) {
		ret = dgx_spbm_limit_range(data, limit, &low, &ceiling);
		if (ret)
			goto unlock;
		if (!low || low > ceiling) {
			ret = -EBADMSG;
			goto unlock;
		}
		if (mw < low || mw > ceiling) {
			ret = -EINVAL;
			goto unlock;
		}
	}

	if (mw == os) {
		if (data->unsettled & BIT(limit))
			ret = dgx_spbm_settle_locked(data, limit);
		goto unlock;
	}

	ret = dgx_spbm_write_limit_locked(data, limit, mw);
	if (!ret) {
		dev_info(data->dev, "%s OS limit=%u mW%s\n", label, mw,
			 mw ? "" : " (NVIDIA default)");
		goto unlock;
	}
	/* Undo only a request that is still ours; never overwrite a racer. */
	if (mw && dgx_spbm_read(data, dgx_spbm_limits[limit].os) == mw) {
		restore_ret = dgx_spbm_write_limit_locked(data, limit, 0);
		if (restore_ret)
			dev_err(data->dev,
				"%s limit change failed (%d), default restore failed (%d)\n",
				label, ret, restore_ret);
	}

unlock:
	mutex_unlock(&data->lock);
	return ret;
}

static int dgx_spbm_restore_locked(struct dgx_spbm_data *data,
				   unsigned int *restored)
{
	unsigned int limit;
	int stale = 0;
	int ret = 0;
	int err;

	for (limit = 0; limit < ARRAY_SIZE(dgx_spbm_limits); limit++) {
		if (!data->owned_mw[limit] && !(data->unsettled & BIT(limit)))
			continue;
		/* Never overwrite a slot another writer changed. */
		if (dgx_spbm_read(data, dgx_spbm_limits[limit].os) !=
		    data->owned_mw[limit]) {
			stale = -ESTALE;
			continue;
		}
		err = data->owned_mw[limit] ?
			dgx_spbm_write_limit_locked(data, limit, 0) :
			dgx_spbm_settle_locked(data, limit);
		if (err)
			ret = err;
		else
			(*restored)++;
	}
	/* Retry transient failures; a foreign slot alone is final. */
	return ret ?: stale;
}

static int dgx_spbm_restore(struct dgx_spbm_data *data, const char *reason,
			    bool final)
{
	unsigned int attempt, restored = 0;
	int ret = 0;

	mutex_lock(&data->lock);
	/* Final restoration fences later sysfs writes before devm teardown. */
	if (final)
		data->control_enabled = false;
	for (attempt = 0; attempt < DGX_SPBM_RESTORE_ATTEMPTS; attempt++) {
		ret = dgx_spbm_restore_locked(data, &restored);
		if (!ret || ret == -ESTALE)
			break;
		if (attempt + 1 < DGX_SPBM_RESTORE_ATTEMPTS)
			msleep(DGX_SPBM_RESTORE_RETRY_MS);
	}
	if (ret)
		dev_err(data->dev,
			"failed to restore NVIDIA power limits for %s: %d\n",
			reason, ret);
	else if (restored)
		dev_info(data->dev,
			 "restored %u NVIDIA default power limit(s) for %s\n",
			 restored, reason);
	else
		dev_info(data->dev,
			 "no driver-owned power limits to restore for %s\n",
			 reason);
	mutex_unlock(&data->lock);

	return ret;
}

static int dgx_spbm_read_mw(const struct dgx_spbm_data *data,
			    enum dgx_spbm_register_id id, long *val)
{
	u32 mw = dgx_spbm_read(data, id);

	if (mw > DGX_SPBM_MAX_PLAUSIBLE_MW)
		return -EBADMSG;
	*val = (long)mw * 1000;
	return 0;
}

static int dgx_spbm_read_power(const struct dgx_spbm_data *data, u32 attr,
			       int channel, long *val)
{
	const struct dgx_spbm_limit *l;
	u32 low, ceiling;
	int ret;

	if (attr == hwmon_power_input)
		return dgx_spbm_read_mw(data, dgx_spbm_power_channels[channel].id,
					val);
	if (channel < DGX_SPBM_FIRST_LIMIT_CHANNEL)
		return -EOPNOTSUPP;

	l = &dgx_spbm_limits[channel - DGX_SPBM_FIRST_LIMIT_CHANNEL];
	switch (attr) {
	case hwmon_power_cap:
		return dgx_spbm_read_mw(data, l->applied, val);
	case hwmon_power_cap_min:
		return dgx_spbm_read_mw(data, l->low, val);
	case hwmon_power_cap_max:
		ret = dgx_spbm_limit_range(data,
					   channel - DGX_SPBM_FIRST_LIMIT_CHANNEL,
					   &low, &ceiling);
		if (!ret)
			*val = (long)ceiling * 1000;
		return ret;
	default:
		return -EOPNOTSUPP;
	}
}

static int dgx_spbm_hwmon_read(struct device *dev,
			       enum hwmon_sensor_types type, u32 attr,
			       int channel, long *val)
{
	struct dgx_spbm_data *data = dev_get_drvdata(dev);
	u32 raw;

	switch (type) {
	case hwmon_power:
		return dgx_spbm_read_power(data, attr, channel, val);
	case hwmon_energy:
		raw = dgx_spbm_read(data, dgx_spbm_energy_channels[channel].id);
		*val = (long)raw * 1000;
		return 0;
	case hwmon_temp:
		raw = dgx_spbm_read(data, dgx_spbm_temp_channels[channel].id);
		if (raw > DGX_SPBM_MAX_PLAUSIBLE_CENTI_C)
			return -EBADMSG;
		*val = (long)raw * 10;
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

static int dgx_spbm_hwmon_read_string(struct device *dev,
				      enum hwmon_sensor_types type, u32 attr,
				      int channel, const char **str)
{
	switch (type) {
	case hwmon_power:
		*str = dgx_spbm_power_channels[channel].label;
		return 0;
	case hwmon_energy:
		*str = dgx_spbm_energy_channels[channel].label;
		return 0;
	case hwmon_temp:
		*str = dgx_spbm_temp_channels[channel].label;
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

static int dgx_spbm_hwmon_write(struct device *dev,
				enum hwmon_sensor_types type, u32 attr,
				int channel, long val)
{
	struct dgx_spbm_data *data = dev_get_drvdata(dev);

	if (type != hwmon_power || attr != hwmon_power_cap ||
	    channel < DGX_SPBM_FIRST_LIMIT_CHANNEL)
		return -EOPNOTSUPP;
	/* Whole milliwatts only; zero restores NVIDIA's default. */
	if (val < 0 || val % 1000 || val / 1000 > DGX_SPBM_MAX_PLAUSIBLE_MW)
		return -EINVAL;

	return dgx_spbm_set_limit(data, channel - DGX_SPBM_FIRST_LIMIT_CHANNEL,
				  val / 1000);
}

static umode_t dgx_spbm_is_visible(const void *drvdata,
				   enum hwmon_sensor_types type, u32 attr,
				   int channel)
{
	const struct dgx_spbm_data *data = drvdata;

	if (type == hwmon_power && attr == hwmon_power_cap &&
	    data->control_enabled)
		return 0644;
	return 0444;
}

static const struct hwmon_ops dgx_spbm_hwmon_ops = {
	.is_visible = dgx_spbm_is_visible,
	.read = dgx_spbm_hwmon_read,
	.read_string = dgx_spbm_hwmon_read_string,
	.write = dgx_spbm_hwmon_write,
};

static const struct hwmon_channel_info * const dgx_spbm_hwmon_info[] = {
	HWMON_CHANNEL_INFO(power,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL,
			   HWMON_P_INPUT | HWMON_P_LABEL | HWMON_P_CAP |
			   HWMON_P_CAP_MAX | HWMON_P_CAP_MIN,
			   HWMON_P_INPUT | HWMON_P_LABEL | HWMON_P_CAP |
			   HWMON_P_CAP_MAX | HWMON_P_CAP_MIN,
			   HWMON_P_INPUT | HWMON_P_LABEL | HWMON_P_CAP |
			   HWMON_P_CAP_MAX | HWMON_P_CAP_MIN,
			   HWMON_P_INPUT | HWMON_P_LABEL | HWMON_P_CAP |
			   HWMON_P_CAP_MAX | HWMON_P_CAP_MIN),
	HWMON_CHANNEL_INFO(energy,
			   HWMON_E_INPUT | HWMON_E_LABEL,
			   HWMON_E_INPUT | HWMON_E_LABEL,
			   HWMON_E_INPUT | HWMON_E_LABEL,
			   HWMON_E_INPUT | HWMON_E_LABEL),
	HWMON_CHANNEL_INFO(temp,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL,
			   HWMON_T_INPUT | HWMON_T_LABEL),
	NULL
};

static const struct hwmon_chip_info dgx_spbm_chip_info = {
	.ops = &dgx_spbm_hwmon_ops,
	.info = dgx_spbm_hwmon_info,
};

/* Dimensionless firmware status, exposed raw as in spark_hwmon. */
static ssize_t dgx_spbm_status_show(struct device *dev,
				    struct device_attribute *attr, char *buf)
{
	struct dgx_spbm_data *data = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%u\n",
			  dgx_spbm_read(data, to_sensor_dev_attr(attr)->index));
}

static SENSOR_DEVICE_ATTR(prochot, 0444, dgx_spbm_status_show, NULL,
			  DGX_SPBM_PROCHOT);
static SENSOR_DEVICE_ATTR(pl_level, 0444, dgx_spbm_status_show, NULL,
			  DGX_SPBM_PL_LEVEL);
static SENSOR_DEVICE_ATTR(tj_max_c, 0444, dgx_spbm_status_show, NULL,
			  DGX_SPBM_TJ_MAX_C);

static struct attribute *dgx_spbm_status_attrs[] = {
	&sensor_dev_attr_prochot.dev_attr.attr,
	&sensor_dev_attr_pl_level.dev_attr.attr,
	&sensor_dev_attr_tj_max_c.dev_attr.attr,
	NULL
};
ATTRIBUTE_GROUPS(dgx_spbm_status);

static bool dgx_spbm_name_is(const union acpi_object *obj, const char *name)
{
	return obj->type == ACPI_TYPE_STRING &&
	       obj->string.length == strlen(name) &&
	       !memcmp(obj->string.pointer, name, obj->string.length);
}

static int dgx_spbm_validate_resource_name(struct device *dev,
					   acpi_handle handle)
{
	union acpi_object *names;
	int ret = 0;

	names = acpi_evaluate_dsm_typed(handle, &dgx_spbm_dsm_guid,
					DGX_SPBM_DSM_REVISION,
					DGX_SPBM_DSM_RESOURCES, NULL,
					ACPI_TYPE_PACKAGE);
	if (!names)
		return -ENODEV;
	if (names->package.count <= DGX_SPBM_RESOURCE_INDEX ||
	    !dgx_spbm_name_is(&names->package.elements[DGX_SPBM_RESOURCE_INDEX],
			      "SPBM")) {
		dev_err(dev, "_DSM resource %u is not SPBM\n",
			DGX_SPBM_RESOURCE_INDEX);
		ret = -ENODEV;
	}
	ACPI_FREE(names);
	return ret;
}

/* Each group is {count, name, offset, ...}.  Unused names are ignored; every
 * pinned register must appear exactly once, at its pinned offset.
 */
static int dgx_spbm_validate_map(struct device *dev,
				 const union acpi_object *map)
{
	const union acpi_object *group, *entry;
	unsigned int g, i, id;
	u64 seen = 0;

	if (map->type != ACPI_TYPE_PACKAGE)
		return -EBADMSG;
	for (g = 0; g < map->package.count; g++) {
		group = &map->package.elements[g];
		if (group->type != ACPI_TYPE_PACKAGE ||
		    !(group->package.count & 1))
			return -EBADMSG;
		entry = group->package.elements;
		if (entry[0].type != ACPI_TYPE_INTEGER ||
		    entry[0].integer.value != group->package.count / 2)
			return -EBADMSG;
		for (i = 1; i < group->package.count; i += 2) {
			if (entry[i].type != ACPI_TYPE_STRING ||
			    entry[i + 1].type != ACPI_TYPE_INTEGER)
				return -EBADMSG;
			for (id = 0; id < DGX_SPBM_REGISTER_COUNT; id++) {
				if (!dgx_spbm_name_is(&entry[i],
						      dgx_spbm_registers[id].name))
					continue;
				if ((seen & BIT_ULL(id)) ||
				    entry[i + 1].integer.value !=
					dgx_spbm_registers[id].offset) {
					dev_err(dev, "register map mismatch: %s\n",
						dgx_spbm_registers[id].name);
					return -EBADMSG;
				}
				seen |= BIT_ULL(id);
			}
		}
	}

	for (id = 0; id < DGX_SPBM_REGISTER_COUNT; id++) {
		if (!(seen & BIT_ULL(id))) {
			dev_err(dev, "register map lacks %s\n",
				dgx_spbm_registers[id].name);
			return -ENOENT;
		}
	}
	return 0;
}

static int dgx_spbm_check_register_map(struct device *dev,
				       acpi_handle handle)
{
	union acpi_object index, argument, *map;
	int ret;

	/* Assign members separately: one union initializer naming both .type
	 * and .integer.value would leave the type zeroed.
	 */
	index.type = ACPI_TYPE_INTEGER;
	index.integer.value = DGX_SPBM_RESOURCE_INDEX;
	argument.type = ACPI_TYPE_PACKAGE;
	argument.package.count = 1;
	argument.package.elements = &index;

	map = acpi_evaluate_dsm_typed(handle, &dgx_spbm_dsm_guid,
				      DGX_SPBM_DSM_REVISION,
				      DGX_SPBM_DSM_REGISTER_MAP, &argument,
				      ACPI_TYPE_PACKAGE);
	if (!map)
		return -ENODEV;
	ret = dgx_spbm_validate_map(dev, map);
	ACPI_FREE(map);
	return ret;
}

static int dgx_spbm_reboot_notify(struct notifier_block *notifier,
				  unsigned long action, void *unused)
{
	struct dgx_spbm_data *data =
		container_of(notifier, struct dgx_spbm_data, reboot_notifier);

	dgx_spbm_restore(data, "reboot", true);
	return NOTIFY_DONE;
}

static int dgx_spbm_suspend(struct device *dev)
{
	struct dgx_spbm_data *data = dev_get_drvdata(dev);

	return dgx_spbm_restore(data, "suspend", false);
}

static DEFINE_SIMPLE_DEV_PM_OPS(dgx_spbm_pm_ops, dgx_spbm_suspend, NULL);

static int dgx_spbm_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct dgx_spbm_data *data;
	struct device *hwmon_dev;
	struct resource *res;
	acpi_handle handle;
	unsigned int limit;
	u32 total, ec, os;
	int ret;

	BUILD_BUG_ON(DGX_SPBM_REGISTER_COUNT > 64);
	BUILD_BUG_ON(ARRAY_SIZE(dgx_spbm_power_channels) !=
		     DGX_SPBM_FIRST_LIMIT_CHANNEL + ARRAY_SIZE(dgx_spbm_limits));

	if (!dgx_spbm_is_supported_platform())
		return -ENODEV;
	handle = ACPI_HANDLE(dev);
	if (!handle ||
	    !acpi_check_dsm(handle, &dgx_spbm_dsm_guid, DGX_SPBM_DSM_REVISION,
			    BIT(DGX_SPBM_DSM_RESOURCES) |
			    BIT(DGX_SPBM_DSM_REGISTER_MAP)))
		return dev_err_probe(dev, -ENODEV,
				     "MTEL register-map _DSM unavailable\n");
	ret = dgx_spbm_validate_resource_name(dev, handle);
	if (ret)
		return dev_err_probe(dev, ret, "refusing SPBM resource\n");
	res = platform_get_resource(pdev, IORESOURCE_MEM,
				    DGX_SPBM_RESOURCE_INDEX);
	if (!res || res->start != DGX_SPBM_BASE ||
	    resource_size(res) != DGX_SPBM_SIZE)
		return dev_err_probe(dev, -ENODEV,
				     "unexpected SPBM resource %pR\n", res);
	ret = dgx_spbm_check_register_map(dev, handle);
	if (ret)
		return dev_err_probe(dev, ret,
				     "SPBM register map outside the pinned contract\n");

	data = devm_kzalloc(dev, sizeof(*data), GFP_KERNEL);
	if (!data)
		return -ENOMEM;
	data->dev = dev;
	mutex_init(&data->lock);
	data->base = devm_ioremap_resource(dev, res);
	if (IS_ERR(data->base))
		return dev_err_probe(dev, PTR_ERR(data->base),
				     "failed to map SPBM\n");
	platform_set_drvdata(pdev, data);

	total = dgx_spbm_read(data, DGX_SPBM_SYS_TOTAL);
	if (!total || total > DGX_SPBM_MAX_PLAUSIBLE_MW)
		return dev_err_probe(dev, -ENODATA,
				     "SPBM telemetry inactive: sys_total=%u mW\n",
				     total);

	data->control_enabled = !read_only;
	for (limit = 0; limit < ARRAY_SIZE(dgx_spbm_limits); limit++) {
		os = dgx_spbm_read(data, dgx_spbm_limits[limit].os);
		ec = dgx_spbm_read(data, dgx_spbm_limits[limit].ec);
		if (os) {
			dev_warn(dev,
				 "existing %s OS limit %u mW; power-limit control disabled\n",
				 dgx_spbm_limit_label(limit), os);
			data->control_enabled = false;
		}
		if (!ec)
			dev_warn(dev,
				 "NVIDIA %s limit unpublished; firmware applies %u mW\n",
				 dgx_spbm_limit_label(limit),
				 dgx_spbm_read(data, dgx_spbm_limits[limit].applied));
	}

	hwmon_dev = devm_hwmon_device_register_with_info(dev, "dgx_spbm_power",
							 data,
							 &dgx_spbm_chip_info,
							 dgx_spbm_status_groups);
	if (IS_ERR(hwmon_dev))
		return dev_err_probe(dev, PTR_ERR(hwmon_dev),
				     "failed to register hwmon\n");

	data->reboot_notifier.notifier_call = dgx_spbm_reboot_notify;
	ret = devm_register_reboot_notifier(dev, &data->reboot_notifier);
	if (ret)
		return dev_err_probe(dev, ret,
				     "failed to register reboot restoration\n");

	dev_info(dev,
		 "SPBM telemetry at %pR: sys_total=%u mW, NVIDIA limits pl1=%u pl2=%u syspl1=%u syspl2=%u mW, control %s\n",
		 res, total,
		 dgx_spbm_read(data, DGX_SPBM_PL1_EC),
		 dgx_spbm_read(data, DGX_SPBM_PL2_EC),
		 dgx_spbm_read(data, DGX_SPBM_SYSPL1_EC),
		 dgx_spbm_read(data, DGX_SPBM_SYSPL2_EC),
		 data->control_enabled ? "available" : "disabled");
	return 0;
}

static void dgx_spbm_remove(struct platform_device *pdev)
{
	struct dgx_spbm_data *data = platform_get_drvdata(pdev);

	dgx_spbm_restore(data, "driver removal", true);
}

/* No MODULE_DEVICE_TABLE: load explicitly, as with the fan-floor driver. */
static const struct acpi_device_id dgx_spbm_acpi_ids[] = {
	{ "NVDA8800" },
	{}
};

static struct platform_driver dgx_spbm_driver = {
	.probe = dgx_spbm_probe,
	.remove = dgx_spbm_remove,
	.driver = {
		.name = "dgx-spbm-power-control",
		.acpi_match_table = dgx_spbm_acpi_ids,
		.pm = pm_sleep_ptr(&dgx_spbm_pm_ops),
	},
};

static int __init dgx_spbm_init(void)
{
	if (!dgx_spbm_is_supported_platform())
		return -ENODEV;
	return platform_driver_register(&dgx_spbm_driver);
}

static void __exit dgx_spbm_exit(void)
{
	platform_driver_unregister(&dgx_spbm_driver);
}

module_init(dgx_spbm_init);
module_exit(dgx_spbm_exit);

MODULE_AUTHOR("Christopher Owen");
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("NVIDIA DGX Spark SPBM power telemetry and restrictive power limits");
MODULE_VERSION(DGX_SPBM_DRIVER_VERSION);

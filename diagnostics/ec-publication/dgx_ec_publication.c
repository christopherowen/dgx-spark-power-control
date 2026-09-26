// SPDX-License-Identifier: GPL-2.0-only
/*
 * One bounded, passive capture for the EC 3.5.8 / SoC 2.155.11 investigation.
 * Use collect.sh for the additional firmware and kernel checks. Not a hwmon
 * driver or recovery interface; deliberately excluded from DKMS/autoload.
 *
 * OEM12 RESP2 does not return the inner EC read status. Transport success and
 * the version canary cannot establish that every individual read succeeded.
 * No EC command packet, setter, event ACK or nonsecure mailbox write is used.
 */
#include <linux/arm_ffa.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/ktime.h>
#include <linux/kobject.h>
#include <linux/module.h>
#include <linux/sysfs.h>
#include <linux/unaligned.h>

#define CAPTURE_SAMPLES 24
#define SOURCE_COUNT 6

struct publication {
	u8 version[5];
	u8 limits[SOURCE_COUNT * sizeof(u32)];
	u8 rtc[6];
};

struct observation {
	u64 start_ns, end_ns;
	u8 status_before, status_after, opcode;
	u8 rtc[6];
	bool packet_changed;
};

static struct publication initial, final;
static struct observation observations[CAPTURE_SAMPLES];
static const u8 expected_version[] = { 3, 5, 8, 0, 0 };
static struct kobject *capture_kobj;
static int probe_result = -ENODEV;
static const struct ffa_device_id ids[] = {
	{ .uuid = UUID_INIT(0x884a63a0, 0x3285, 0x4120,
			   0x83, 0xaa, 0xee, 0xc0, 0x08, 0xa0, 0xa5, 0x46) },
	{ }
};

/* No address or length can be supplied through a userspace interface. */
static int read_fixed(struct ffa_device *dev, u32 address, u32 length, u8 *out)
{
	struct ffa_send_direct_data2 msg = { 0 };
	u8 *raw = (u8 *)msg.data;
	int ret;

	/* 0x06000500 consumes an event: never read it here. */
	if (!((address == 0x06000760 && length == 5) ||
	      (address == 0x06000714 && length == 24) ||
	      (address == 0x06000788 && length == 6) ||
	      (address == 0x06000800 && length == 8) ||
	      (address == 0x06000504 && length == 1)))
		return -EINVAL;
	raw[0] = 12;
	put_unaligned_le32(address, raw + 1);
	put_unaligned_le32(length, raw + 5);
	ret = dev->ops->msg_ops->sync_send_receive2(dev, &msg);
	if (!ret)
		memcpy(out, raw, length);
	return ret;
}

static int read_publication(struct ffa_device *dev, struct publication *p)
{
	int ret;

	ret = read_fixed(dev, 0x06000760, sizeof(p->version), p->version);
	if (ret)
		return ret;
	if (memcmp(p->version, expected_version, sizeof(expected_version)))
		return -EBADMSG;
	ret = read_fixed(dev, 0x06000714, sizeof(p->limits), p->limits);
	if (ret)
		return ret;
	return read_fixed(dev, 0x06000788, sizeof(p->rtc), p->rtc);
}

static int capture_probe(struct ffa_device *dev)
{
	u8 packet[8], previous[8] = { 0 };
	unsigned int i;
	int ret;

	if (!uuid_equal(&dev->uuid, &ids[0].uuid) || dev->vm_id != 0x8003 ||
	    dev->properties != 0x0109 || dev->mode_32bit || !dev->ops ||
	    !dev->ops->info_ops || !dev->ops->info_ops->api_version_get ||
	    dev->ops->info_ops->api_version_get() != FFA_VERSION_1_2 ||
	    !dev->ops->msg_ops || !dev->ops->msg_ops->sync_send_receive2)
		return -ENODEV;
	ret = read_publication(dev, &initial);
	if (ret)
		return ret;
	for (i = 0; i < CAPTURE_SAMPLES; i++) {
		struct observation *s = &observations[i];

		s->start_ns = ktime_get_ns();
		ret = read_fixed(dev, 0x06000504, 1, &s->status_before);
		if (ret)
			return ret;
		ret = read_fixed(dev, 0x06000800, sizeof(packet), packet);
		if (ret)
			return ret;
		s->opcode = packet[0];
		s->packet_changed = i && memcmp(packet, previous, sizeof(packet));
		memcpy(previous, packet, sizeof(packet));
		ret = read_fixed(dev, 0x06000788, sizeof(s->rtc), s->rtc);
		if (ret)
			return ret;
		ret = read_fixed(dev, 0x06000504, 1, &s->status_after);
		if (ret)
			return ret;
		s->end_ns = ktime_get_ns();
		if (i + 1 < CAPTURE_SAMPLES)
			msleep(317);
	}
	ret = read_publication(dev, &final);
	if (ret)
		return ret;
	probe_result = 0;
	dev_info(&dev->dev, "passive publication capture complete; inner EC read status unavailable\n");
	return 0;
}

static ssize_t publication_show(const struct publication *p, char *buf)
{
	return sysfs_emit(buf,
		"version=%5phN source_mw=%u,%u,%u,%u,%u,%u rtc_bcd=%6phN\n",
		p->version, get_unaligned_le32(p->limits),
		get_unaligned_le32(p->limits + 4), get_unaligned_le32(p->limits + 8),
		get_unaligned_le32(p->limits + 12), get_unaligned_le32(p->limits + 16),
		get_unaligned_le32(p->limits + 20), p->rtc);
}

static ssize_t initial_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
	return publication_show(&initial, buf);
}

static ssize_t final_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
	return publication_show(&final, buf);
}

static ssize_t trace_show(struct kobject *k, struct kobj_attribute *a, char *buf)
{
	unsigned int i;
	ssize_t n = sysfs_emit(buf,
		"schema=1 samples=24 submitted_packets=0 inner_status_available=0\n");

	for (i = 0; i < CAPTURE_SAMPLES; i++) {
		const struct observation *s = &observations[i];

		n += sysfs_emit_at(buf, n,
			"%u start_ns=%llu duration_ns=%llu status=%02x,%02x opcode=%02x packet_changed=%u rtc_bcd=%6phN\n",
			i, s->start_ns, s->end_ns - s->start_ns,
			s->status_before, s->status_after, s->opcode,
			s->packet_changed, s->rtc);
	}
	return n;
}

static struct kobj_attribute initial_attr = __ATTR_RO_MODE(initial, 0400);
static struct kobj_attribute final_attr = __ATTR_RO_MODE(final, 0400);
static struct kobj_attribute trace_attr = __ATTR_RO_MODE(trace, 0400);
static struct attribute *attrs[] = {
	&initial_attr.attr, &trace_attr.attr, &final_attr.attr, NULL
};
static const struct attribute_group group = { .attrs = attrs };

static void capture_remove(struct ffa_device *dev) { }

static struct ffa_driver capture_driver = {
	.name = "dgx-ec-publication", .probe = capture_probe,
	.remove = capture_remove, .id_table = ids,
};

static int __init capture_init(void)
{
	int ret;

	if (!dmi_match(DMI_SYS_VENDOR, "NVIDIA") ||
	    !dmi_match(DMI_PRODUCT_NAME, "NVIDIA_DGX_Spark") ||
	    !dmi_match(DMI_BOARD_NAME, "P4242"))
		return -ENODEV;
	ret = ffa_register(&capture_driver);
	if (ret)
		return ret;
	if (probe_result) {
		ret = probe_result;
		goto unregister;
	}
	capture_kobj = kobject_create_and_add("dgx_ec_publication", kernel_kobj);
	if (!capture_kobj) {
		ret = -ENOMEM;
		goto unregister;
	}
	ret = sysfs_create_group(capture_kobj, &group);
	if (!ret)
		return 0;
	kobject_put(capture_kobj);
unregister:
	ffa_unregister(&capture_driver);
	return ret;
}

static void __exit capture_exit(void)
{
	sysfs_remove_group(capture_kobj, &group);
	kobject_put(capture_kobj);
	ffa_unregister(&capture_driver);
}

module_init(capture_init);
module_exit(capture_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Bounded passive EC publication diagnosis, not recovery");
MODULE_VERSION("0.1.0");

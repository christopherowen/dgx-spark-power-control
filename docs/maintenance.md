# Updates and removal

[← README](../README.md) · [Installation](installation.md) · [Usage](usage.md)

A module file is built for one kernel. Installing a new file does not replace a
module already loaded in memory. Keep NVIDIA's normal OS and firmware update
process; after either, check that this driver still binds.

## Kernel updates

With [DKMS](installation.md#3a-dkms-builds-for-kernel-updates), package hooks
normally rebuild and sign the registered source for new kernels. Before
rebooting into one, set its **exact installed release name** and verify:

```sh
# Replace the example with the kernel you are about to boot.
new_kernel="7.0.0-1019-nvidia"
sudo apt-get install "linux-headers-$new_kernel"
sudo dkms install -m dgx-spark-power-control -v 0.1.0 -k "$new_kernel"
modinfo -k "$new_kernel" -F vermagic dgx_spbm_power_control
modinfo -k "$new_kernel" -F signer dgx_spbm_power_control
```

For manual builds, run from your checkout:

```sh
KERNEL_RELEASE="$new_kernel" ./scripts/build-sign
sudo install -D -m 0644 kernel/dgx_spbm_power_control.ko \
  "/lib/modules/$new_kernel/updates/dgx_spbm_power_control.ko"
sudo depmod -a "$new_kernel"
```

After booting, check `dgx-power-control status` and
`sudo journalctl -k -b | grep -i spbm`. After a **firmware** update, also
check that probe did not refuse a changed register map.

## Update this project's source

Restore NVIDIA's limits and unload the existing module first:

```sh
sudo dgx-power-control automatic
sudo modprobe -r dgx_spbm_power_control
git pull --ff-only
./scripts/check
```

Then repeat your installation route. For DKMS, remove the old registered
version (`dkms status` shows it) with `sudo dkms remove -m
dgx-spark-power-control -v <old version> --all`. Then repeat the source-copy,
add, build, and install steps with the version in `dkms.conf`. Finally,
reinstall the command and reload:

```sh
sudo install -D -m 0755 userspace/dgx_power_control.py /usr/local/sbin/dgx-power-control
sudo modprobe dgx_spbm_power_control
dgx-power-control status
```

## Uninstall

```sh
sudo dgx-power-control automatic
sudo modprobe -r dgx_spbm_power_control
sudo dkms remove -m dgx-spark-power-control -v 0.1.0 --all   # DKMS route
sudo rm -f "/lib/modules/$(uname -r)/updates/dgx_spbm_power_control.ko"  # manual route
sudo depmod -a
sudo rm -f /etc/modules-load.d/dgx_spbm_power_control.conf /usr/local/sbin/dgx-power-control
```

Removing the module also restores any limit it set; check the kernel log
for `restored` or `no driver-owned power limits`. Keep signing keys and DKMS
signing settings if other modules, such as the fan driver, use them.

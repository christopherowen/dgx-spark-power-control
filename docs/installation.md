# Install on a DGX Spark

[← README](../README.md) · [Usage](usage.md) · [Updates and removal](maintenance.md)

Run commands on the Spark itself. If you have not enrolled a module-signing
key before, plan console access: enrollment includes a firmware-screen step
during reboot.

The order is: **get the source → use or enroll a signing key → choose DKMS or
manual build → load and verify → choose startup behavior**.

## 1. Get the source and tools

```sh
sudo apt-get update
sudo apt-get install git build-essential linux-headers-"$(uname -r)" python3 openssl mokutil
git clone https://github.com/christopherowen/dgx-spark-power-control.git
cd dgx-spark-power-control
./scripts/check
uname -r
mokutil --sb-state
```

Keep this checkout for builds and updates. Commands below assume you are in its
root. Another kernel's headers are not a substitute for matching ones.

## 2. Use an enrolled signing key

If you already installed
[dgx-spark-fan-control](https://github.com/christopherowen/dgx-spark-fan-control),
reuse its enrolled pair. Do not enroll a second key:

```sh
export DGX_MOK_DIR="$HOME/.local/share/dgx-spark-fan-control/keys"
mokutil --test-key "$DGX_MOK_DIR/MOK.der"
```

Expect a message saying the certificate is already enrolled. Any other enrolled
`MOK.priv` / `MOK.der` pair works the same way through `DGX_MOK_DIR`.

### Or create and enroll a key, once

```sh
./scripts/generate-signing-key
sudo mokutil --import "${DGX_MOK_DIR:-$HOME/.local/share/dgx-spark-power-control/keys}/MOK.der"
sudo systemctl reboot
```

The script writes `MOK.priv` (mode `0600`; keep private) and `MOK.der` outside
the checkout and refuses to overwrite an existing pair. Choose a temporary
password for `--import`. At the firmware MOK manager, select **Enroll MOK →
Continue → Yes** and enter it. SSH alone cannot complete this step. After
reboot, verify with `mokutil --test-key` as above.

A machine intentionally running without signature enforcement can skip
enrollment; changing Secure Boot settings is not required by this project.

## 3A. DKMS: builds for kernel updates

Choose **this route or the manual route**, not both. DKMS rebuilds registered
source for newly installed kernels; matching headers are still required.
[`dkms.conf`](../dkms.conf) builds only this module, for aarch64.

DKMS signs with its **system-wide** key. If you already configured it for the
fan driver, keep that setting. Otherwise, follow the fan project's
[DKMS signing identity steps](https://github.com/christopherowen/dgx-spark-fan-control/blob/main/docs/installation.md#set-dkmss-persistent-signing-identity),
using your enrolled pair. Then register only the files needed to build:

```sh
sudo install -d /usr/src/dgx-spark-power-control-0.1.0/kernel
sudo install -m 0644 dkms.conf /usr/src/dgx-spark-power-control-0.1.0/dkms.conf
sudo install -m 0644 kernel/Makefile kernel/dgx_spbm_power_control.c \
  /usr/src/dgx-spark-power-control-0.1.0/kernel/
sudo dkms add -m dgx-spark-power-control -v 0.1.0
sudo dkms build -m dgx-spark-power-control -v 0.1.0 -k "$(uname -r)"
sudo dkms install -m dgx-spark-power-control -v 0.1.0 -k "$(uname -r)"
dkms status -m dgx-spark-power-control
modinfo -F signer dgx_spbm_power_control
```

Expect `installed` for the running kernel and the intended certificate subject
in `signer`. Continue with step 4.

## 3B. Manual build and sign

This route requires repeating the build/install steps for each new kernel:

```sh
./scripts/build-sign
sudo install -D -m 0644 kernel/dgx_spbm_power_control.ko \
  "/lib/modules/$(uname -r)/updates/dgx_spbm_power_control.ko"
sudo depmod -a
modinfo -F signer dgx_spbm_power_control
```

`build-sign` requires an enrolled certificate and a `0600` private key, builds
with `W=1` for the running kernel, and never installs or loads a module. Run
it as the user who owns the key. Without signature enforcement, use
`make -C kernel` instead, then the same installation commands.

## 4. Load the driver and verify

Both build routes join here. Do not load this alongside
[spark_hwmon](https://github.com/antheas/spark_hwmon)'s `spbm` module:

```sh
lsmod | grep -w spbm && sudo modprobe -r spbm
sudo install -D -m 0755 userspace/dgx_power_control.py /usr/local/sbin/dgx-power-control
sudo modprobe dgx_spbm_power_control
dgx-power-control status
sudo journalctl -k -b | grep -i spbm
```

Status must show nonzero power readings and **`control=available`**, and each
`cap` should equal its `nvidia` value. A successful `modprobe` alone is
insufficient: probe can still reject the firmware contract. See
[troubleshooting](troubleshooting.md) if status fails, shows
`control=disabled`, or reports `nvidia=unpublished`.

If `spark_hwmon` previously set an OS limit, this driver binds read-only with
`control=disabled`. It never adopts or overwrites another writer's limit.

## 5. Choose startup behavior

To load the driver at boot:

```sh
sudo install -D -m 0644 systemd/dgx_spbm_power_control.conf \
  /etc/modules-load.d/dgx_spbm_power_control.conf
```

The module loads with NVIDIA's limits in effect. There is no service: limits
you set last until you restore them, remove the driver, suspend, or reboot.
Continue to [everyday usage](usage.md).

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Requires the already built and signed module alongside this script.
set -euo pipefail
umask 077
export LC_ALL=C
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $# == 0 && $EUID == 0 ]] || fail "Run as root with no arguments."
exec 9>/run/lock/dgx-ec-publication.lock
flock -n 9 || fail "Another publication capture is running."
[[ $(uname -r) == 7.0.0-1019-nvidia ]] || fail "Unvalidated kernel."
[[ $(cat /sys/class/dmi/id/sys_vendor) == NVIDIA &&
   $(cat /sys/class/dmi/id/product_name) == NVIDIA_DGX_Spark &&
   $(cat /sys/class/dmi/id/board_name) == P4242 ]] || fail "Unvalidated board."
dsdt_hash=1009b95258a5fa8c85fe458b3019cd5658f099c3ba703cec3ec945b5df808f9d
[[ $(sha256sum /sys/firmware/acpi/tables/DSDT | cut -d ' ' -f 1) == "$dsdt_hash" ]] ||
    fail "Unvalidated ACPI table."
inventory=$(dmidecode -t 45)
printf '%s\n' "$inventory" | awk '
    /^[[:space:]]*Firmware Component Name:/ { name=$0; sub(/^.*Name: /, "", name) }
    /^[[:space:]]*Firmware Version:/ {
        version=$0; sub(/^.*Version: /, "", version)
        if (name == "FLASH") { soc++; if (version != "SBP:R:2.155.11") bad=1 }
        if (name == "EC Firmware") { ec++; if (version != "3.5.8") bad=1 }
    }
    END { exit !(soc == 1 && ec == 1 && !bad) }
' || fail "Unvalidated SoC/EC inventory."
awk '/MemAvailable:/ { seen=1; enough=($2 >= 5242880) }
     END { exit !(seen && enough) }' /proc/meminfo || fail "Less than 5 GiB available."
workers=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits)
[[ -z $workers ]] || fail "Compute workers are present; collect between jobs."
[[ ! -d /sys/module/dgx_ec_publication ]] || fail "Probe already loaded."
endpoint=
for candidate in /sys/bus/arm_ffa/devices/*; do
    [[ -r $candidate/modalias ]] || continue
    alias=$(cat "$candidate/modalias")
    # Different UUID endpoints can share the same firmware partition/transport.
    if [[ $alias == arm_ffa:8003:* && -L $candidate/driver ]]; then
        fail "Firmware partition 8003 has a bound Linux client: $candidate."
    fi
    if [[ $alias == arm_ffa:8003:884a63a0-3285-4120-83aa-eec008a0a546 ]]; then
        [[ -z $endpoint ]] || fail "Multiple matching endpoints."
        endpoint=$candidate
    fi
done
[[ -n $endpoint && ! -L $endpoint/driver ]] || fail "OEM endpoint absent or already bound."
module="$root/dgx_ec_publication.ko"
[[ -f $module && -n $(modinfo -F signer "$module") ]] || fail "Build and sign the probe first."
boot_before=$(cat /proc/sys/kernel/random/boot_id)
loaded=0
cleanup() {
    if [[ $loaded == 1 && -d /sys/module/dgx_ec_publication ]]; then
        rmmod dgx_ec_publication || { printf 'Probe removal failed.\n' >&2; return 1; }
        loaded=0
    fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
printf 'capture_format=ec-publication-v2\n'
printf 'captured_at=%s\n' "$(date --iso-8601=seconds)"
printf 'module_sha256=%s\n' "$(sha256sum "$module" | cut -d ' ' -f 1)"
printf 'boot_id=%s\n' "$boot_before"
loaded=1
insmod "$module"
for part in initial trace final; do
    printf 'capture_part=%s\n' "$part"
    cat "/sys/kernel/dgx_ec_publication/$part"
done
cleanup
[[ ! -d /sys/module/dgx_ec_publication && ! -L $endpoint/driver ]] ||
    fail "Probe cleanup could not be verified."
[[ $(cat /proc/sys/kernel/random/boot_id) == "$boot_before" ]] || fail "Boot ID changed."
printf 'probe_unloaded=1 oem_unbound=1 boot_unchanged=1\n'

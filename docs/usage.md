# Power telemetry and limits for everyday work

[← README](../README.md) · [Installation](installation.md) · [Maintenance](maintenance.md)

## Read the telemetry

```sh
dgx-power-control status
watch -n 1 dgx-power-control status
sensors 'dgx_spbm_power-*'        # if lm-sensors is installed
```

The firmware refreshes the page continuously; each read returns its latest
values. Readings from different channels are not an atomic snapshot.
Instantaneous power oscillates with the firmware's control loop (100 ms,
according to spark_hwmon). For averages, difference the **energy counters**
over a known interval instead of sampling power:

```sh
for device in /sys/class/hwmon/hwmon*; do
  if [ "$(cat "$device/name" 2>/dev/null)" = dgx_spbm_power ]; then
    start=$(cat "$device/energy1_input"); sleep 10; end=$(cat "$device/energy1_input")
    echo "package average: $(( (end - start) / 10000000 )) W"
  fi
done
```

Find the device by name, as above: numeric hwmon indexes change across boots.
The energy counters are 32-bit firmware millijoules. They wrap every
2^32 mJ (about 4.29 MJ, or 12 hours at 100 W), so keep intervals short and
treat a decrease as wraparound. The driver does not interpret the firmware's
overflow registers.

## The four limits

| Limit | Conventional meaning (not vendor-documented) | NVIDIA value on the reference Sparks |
| --- | --- | ---: |
| `pl1` | Sustained SoC package power | 140 W |
| `pl2` | Short-term SoC package power | 142 W |
| `syspl1` | Sustained system power | 231 W |
| `syspl2` | Short-term system power | 244 W |

The firmware's controllers compare an averaged power, reported as each limit
channel's `power_input`, with the applied limit. Apart from the four OS
slots, those controllers remain NVIDIA's.

## Lower a limit for a job

```sh
sudo dgx-power-control set-limit syspl1 150
dgx-power-control status
```

Watts may have up to three decimal places. The accepted range is from the
firmware floor (`floor=`) to **NVIDIA's lowest published limit** (`nvidia=`).
The driver writes the OS slot, requests a firmware update, and waits up to one
second for the applied limit to equal your value. On disagreement it restores
NVIDIA's value and reports an error; the command also checks readback.

Lower limits reduce clocks under load; they do not reduce idle power.
Performance and thermal effects depend on the workload. Set `pl1` no higher
than `pl2`, and `syspl1` no higher than `syspl2`: the driver does not reorder
them, and the firmware's handling of inverted limits is unverified.

## Return to NVIDIA's limits

```sh
sudo dgx-power-control automatic           # all four limits
sudo dgx-power-control automatic syspl1    # or selected limits
dgx-power-control status
```

Each `cap` should equal its `nvidia` value. Limits also return to NVIDIA's on
driver removal, suspend, and orderly reboot. They do not expire, and a hard
crash can skip restoration; check status after any interruption.

## Wrap a job with ordinary-exit cleanup

This **Bash subshell** caps sustained system power, runs a job, and attempts
to restore NVIDIA's limits on exit, including job failure and handled
Ctrl+C/SIGTERM. Replace the marked job command:

```bash
(
  set -e
  sudo -v

  restore_nvidia() {
    job_status=$?
    trap - EXIT
    if ! sudo dgx-power-control automatic; then
      echo "Restoration failed; check dgx-power-control status and the kernel log." >&2
      exit 1
    fi
    exit "$job_status"
  }
  trap restore_nvidia EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  sudo dgx-power-control set-limit syspl1 150
  ./your-job --your-options  # replace this line; run the workload without sudo
)
```

`sudo` may request your password again after a long job. SIGKILL or loss of
the shell prevents cleanup; always verify status after interruption.

## Attribute reference

All power values are microwatts, energy microjoules, and temperatures
millidegrees Celsius, per the Linux hwmon ABI. Channels are identified by
label (`powerN_label`); numbering follows spark_hwmon but is not guaranteed.

| Attribute | Limit channels only | Meaning |
| --- | :---: | --- |
| `powerN_input` | | Power; for limits, the controller's averaged input |
| `powerN_cap` | yes | Firmware-applied limit; `0644` when control is available |
| `powerN_cap_max` | yes | Lowest published NVIDIA limit; `ENODATA` if EC's is unpublished |
| `powerN_cap_min` | yes | Firmware floor |
| `energyN_input` | | Cumulative energy |
| `tempN_input` | | Temperature |
| `prochot`, `pl_level`, `tj_max_c` | | Raw firmware status values |

Writing `powerN_cap` accepts whole milliwatts within `cap_min`..`cap_max`, or
`0` to restore NVIDIA's value. Errors:

| Error | Meaning |
| --- | --- |
| `EINVAL` | Outside the accepted range, negative, or finer than a milliwatt |
| `ENODATA` | NVIDIA's EC limit is unpublished; see [troubleshooting](troubleshooting.md#nvidia-limits-unpublished) |
| `EBADMSG` | Implausible firmware range values |
| `ESTALE` | Another writer changed this OS slot; it is left untouched |
| `ETIMEDOUT` | Firmware did not apply the value within one second; it was undone |
| `EPERM` | Control disabled, or the driver is being removed |

`prochot` has read `1` on healthy and restricted Sparks alike; it is not
diagnostic by itself. `tj_max_c` semantics are unknown.

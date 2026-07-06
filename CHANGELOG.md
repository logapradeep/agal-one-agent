# Changelog — agal-one-agent

## 0.1.7 (unreleased, 2026-07-06)

- **New sensor driver: BNO055 9-DoF IMU** (`sensor_type: "bno055_9dof"`,
  `agal_one_agent/sensors/bno055.py`). NDOF fusion mode; emits
  `{label}.orientation.heading` / `.orientation.roll` / `.orientation.pitch`
  (deg), `{label}.calibration` (overall 0–3 level + per-subsystem
  sys/gyro/accel/mag detail), and `{label}.temperature` (degC). I2C address
  0x28 default, 0x29 via `sensor_params.i2c_address` (or the generic
  `pin.i2c_address`). Reads are wrapped with 2 retries on OSError/timeout to
  tolerate the BNO055's I2C clock stretching; nodes should additionally use
  `dtoverlay=i2c-gpio` (software bus) or `dtparam=i2c_arm_baudrate=10000` in
  boot config — see the module docstring and `sensors/README.md`.
- setup.py: added `adafruit-blinka` + `adafruit-circuitpython-bno055` to
  `extras_require["rpi"]`.

### Version-number reconciliation note

`v0.1.6` ("config-driven telemetry ingress URL + agal-one-agent entry point",
commit `25cd7f3`, tagged on origin/main and deployed to nodes 2026-07-05) was
released from the **pre-rename `menvayal_agent` lineage** and is *not yet
merged* into this `refactor/rename-to-agal-one-agent` branch — the working
tree here still read `0.1.5` before this change. To avoid colliding with the
released tag, this branch jumps straight to `0.1.7`. **Before tagging v0.1.7
for OTA, merge/port the v0.1.6 changes (notably the config-driven
`telemetry.ingress_url`) into this branch**, or nodes updating 0.1.6 → 0.1.7
would regress the 2026-07-05 provisioning fixes.

## 0.1.6 (2026-07-05, released from origin/main — not in this branch yet)

- Config-driven telemetry ingress URL; `agal-one-agent` console-script alias.

## 0.1.5 and earlier

See git tags `v0.1.0` … `v0.1.5` (commit messages carry the summaries).

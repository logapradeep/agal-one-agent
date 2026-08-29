# agal-agent bench-test report — 2026-05-31

> **UPDATE 2026-06-02 — rename completed; finalized as `agal-one-agent`.** Every §2 lingering-`Menvayal` gap below is now resolved, and the target name was changed `agal-agent` → **`agal-one-agent`** to match the `agal.one` family. Throughout this (historical) report, mentally substitute `agal-agent` → `agal-one-agent`, `agal_agent` → `agal_one_agent`, `MenvayalMqttClient` → `AgalOneMqttClient`, and `Agal/daemon/agal-agent` → `Agal/daemon/agal-one-agent`. The no-hardware suite now passes **21/21** via `cd Agal/daemon/agal-one-agent && python3 -m pytest tests/ -q`; the §3 hardware bench-test is still owed. GitHub repo renamed `logapradeep/menvayal-agent` → `logapradeep/agal-one-agent`.

Auditor: Claude Code (claude-opus-4-7), running on macOS, no physical Raspberry Pi available.

## Scope and disclosure

The 6-step bench procedure in [`agal_agent/sensors/README.md`](agal_agent/sensors/README.md) requires physical hardware (RPi, ACS758LCB-050B + ADS1115, JSN-SR04T, relay HAT, contactor, pump or resistive load, multimeter, clamp ammeter, water bucket). **I cannot physically execute steps 1–6 from this session.** The deliverables that don't require silicon — rename-gap audit, code-level issues, no-HW unit tests — were run.

**Treat this report as: "static audit complete; hardware bench-test still owed."** Whoever next sits at the bench should clear the items in §3 *before* touching the wiring, because at least two of them will cause the bench-test to misbehave or be uninterpretable.

## 1. No-hardware test suite — PASS

```
cd Agal/daemon/agal-agent && python3 -m pytest tests/ -v
============================== 8 passed in 1.36s ===============================
```

All 8 protection logic tests pass (`test_protection_no_hw.py`): baseline learning post-inrush, dry-run cutoff after confirm-window, inrush-window suppression, recovery-before-confirm, relay-write-failure still emits event, low-water below threshold fires, low-water cooldown is respected, above-threshold doesn't fire. The decision tree is intact; the bug surface that remains is in I/O around it, not the algorithm.

## 2. Rename-gap audit — **35 lingering `Menvayal` references**

The directory and Python module were renamed (`menvayal-agent` → `agal-agent`, `menvayal_agent` → `agal_agent`), but the rename did NOT propagate to identifiers, docstrings, container metadata, or systemd labels. Earlier reporting that this was clean was incorrect — my first grep used BSD-incompatible `grep -v` syntax that suppressed real matches.

### 2.1 Python identifier — REAL CODE GAP

The MQTT client class is still named `MenvayalMqttClient`:

| File:line | Reference |
|---|---|
| `agal_agent/mqtt_client.py:16` | `class MenvayalMqttClient:` (definition) |
| `agal_agent/command_executor.py:7,74` | `from .mqtt_client import MenvayalMqttClient` + type hint |
| `agal_agent/heartbeat.py:10,26` | import + type hint |
| `agal_agent/telemetry_publisher.py:8,16` | import + type hint |
| `agal_agent/boot_reconciler.py:9,16` | import + type hint |
| `agal_agent/main.py:10,69` | import + instantiation |

This isn't merely cosmetic. Logs grep'd by "Menvayal" will still surface daemon output; any external observability tooling keyed on class name (Sentry breadcrumbs, structured-log filters) silently slides past the rename. Recommended fix: rename to `AgalMqttClient` and add an `import-as` alias in `mqtt_client.py` for one release if any external consumer might import the old name.

### 2.2 Docstrings and module-level strings

| File:line | Reference |
|---|---|
| `agal_agent/__init__.py:1` | `"""Menvayal IoT Agent - connects hardware to Menvayal cloud via MQTT."""` |
| `agal_agent/config.py:1` | `"""YAML configuration loader for Menvayal Agent."""` |
| `agal_agent/main.py:1,28,46` | module docstring + argparse `description=` + startup log line `Menvayal Agent v0.1.5 starting` |
| `agal_agent/http_reporter.py:1` | `"""HTTP reporter for sending telemetry and status to the Menvayal backend."""` |
| `agal_agent/ota_updater.py:1` | `"""OTA (Over-The-Air) self-update for the Menvayal agent."""` |
| `agal_agent/lora_bridge.py:1,9,15,16,33,186` | module docstring (multiple lines) + class docstring + comment |
| `agal_agent/sentry_setup.py:155` | `"[smoke-test] Menvayal daemon Sentry wired up"` |

### 2.3 Container / deployment

| File:line | Reference |
|---|---|
| `Dockerfile:3` | `# Menvayal Agent container — built for balena fleet deployment.` |
| `balena.yml:4` | `Menvayal IoT agent — runs on every farmer-installed Raspberry Pi.` |
| `docker-compose.yml:3` | `# balena-compose for Menvayal Agent fleet.` |
| `systemd/agal-agent.service:2` | `Description=Menvayal IoT Agent` |
| `scripts/install.sh:4` | `echo "=== Menvayal Agent Installer ==="` |

systemd Description and install banner are cosmetic; balena fleet metadata may matter for the cloud dashboard.

### 2.4 Wrong-path documentation — REAL GAP

Three files reference a path that **does not exist** (it's `Agal/daemon/agal-agent`, not `Menvayal/daemon/agal-agent`):

| File:line | Wrong path |
|---|---|
| `BALENA.md:47` | `cd Menvayal/daemon/agal-agent` |
| `BALENA.md:84` | `cd Menvayal/daemon/agal-agent` |
| `tests/test_protection_no_hw.py:4` | docstring header pointing at `Menvayal/daemon/agal-agent` |

Anyone following BALENA.md will `cd` into a directory that doesn't exist and the install will fail at the first command.

## 3. Code-level issues found pre-bench — these will misbehave on real hardware

### 3.1 ~~Baseline persistence is missing~~ **FIXED 2026-05-31** — `current_sensor.py`

**Resolution.** Added `baseline_dir` constructor arg (default `/var/lib/agal-agent/`), `_load_baseline_if_present()` called from `__init__`, `_save_baseline()` called from `learn_baseline()` (atomic write via `.tmp` + `replace`), `clear_baseline()` helper for operator-initiated re-learn. Sanitizes source_key for filesystem safety (`pump1.current` → `baseline.pump1.current.json`). Per-sensor override via `pin.sensor_params["baseline_dir"]`. Best-effort on I/O failure (mkdir/read/write/delete all wrapped — warning logged, daemon continues with in-memory-only baseline). `baseline_dir=None` disables persistence entirely (used in tests).

7 new tests in `tests/test_protection_no_hw.py` cover:
- baseline persists across sensor instances (the launch-blocker scenario)
- `baseline_dir=None` disables persistence cleanly
- corrupt baseline JSON ignored without crashing
- invalid `baseline_a` (zero / negative / missing) rejected
- unwritable `/var/lib/agal-agent-nonexistent-xyz-test` does not crash `learn_baseline()`
- source_key sanitization for filesystem safety (slashes / spaces → `_`)
- `clear_baseline()` deletes the persisted file and is idempotent

**Motor-swap invalidation was deliberately deferred** — the original report suggested gating on `motorConfig.updatedAt` from the asset config. v1 ships without that; operators clear the baseline manually (or via the daemon's new `clear_baseline()` method when a CLI / cloud command exposes it). The dry-run threshold is a percentage of baseline, so a mildly stale baseline still cuts; the only loss is precision of the cutoff window after a motor swap.

### 3.2 ~~MQTT telemetry/status payloads have no `type` discriminator~~ **FIXED 2026-05-31** — `mqtt_client.py`

**Resolution.** Wrapped both `publish_telemetry` and `publish_status` in the canonical `{type, payload: {...}}` envelope the cloud `telemetryIngress` webhook expects (it destructures `const {type, payload} = req.body` at `telemetryIngress.ts:120` and returns 400 if either is missing; `handleTelemetry(db, payload)` then reads `nodeUid`/`readings`/`timestamp` from `payload`). Matches the HTTPS fallback in `http_reporter.py` lines 24, 35, 54 — both transports now hand the same shape to the cloud.

**Defensive — does not require knowing whether a HiveMQ Data Hub policy exists.** If a topic-based policy was injecting `type` and routing `req.body.payload`, the daemon now matches that contract directly without depending on it. If no policy existed (the silent-bug scenario), MQTT telemetry now works for the first time. Either way the bench-test can move forward.

The other publishers were already aligned with their cloud handlers and weren't touched:
- `publish_event` (`type: "sensor_event"`, flat shape) — `handleSensorEvent` reads `req.body as SensorEventPayload`
- `publish_lora_uplink` / `publish_lora_event` (flat with `type`) — corresponding handlers read `req.body`
- `publish_command_ack` (already wrapped `{type, payload}`) — `handleCommandAck` reads `payload`
- Last Will and Testament (lines 47–54) — already wrapped correctly

6 new tests in `tests/test_mqtt_envelope.py` pin the wire contract for each publisher so a future refactor can't silently regress it. The flat-vs-wrapped split between `sensor_event`/`lora_*` and `telemetry`/`status`/`commandAck` reflects the cloud-side handler signatures and is explicitly asserted (a regression that wraps `publish_event` would now break the cloud handler and the test would catch it).

**Followup the report still flags:** ask the HiveMQ Cloud admin to take a screenshot of any Data Hub policy on `agal/{nodeUid}/telemetry` and commit it under `Agal/daemon/agal-agent/docs/hivemq-policies/` so the next-session auditor doesn't have to reason about whether the broker is doing transformations. Not bench-blocking; rename-period housekeeping.

### 3.3 **`install.sh:34` silently swallows pip errors**

```bash
/opt/agal-agent/venv/bin/pip install --quiet \
    paho-mqtt PyYAML RPi.GPIO gpiozero smbus2 \
    spidev pyserial w1thermsensor 2>/dev/null || true
```

`2>/dev/null || true` means a missing package, a wheel-build failure, or even a connectivity blip during `pip install` leaves the venv **partially populated** but the script reports success. The daemon then crashes at first import with a `ModuleNotFoundError` that the operator has to debug from scratch.

**Fix**: drop the `2>/dev/null || true`. If a particular package is allowed to be missing (e.g. `RPi.GPIO` on a non-Pi dev box), install it as a separate command with explicit error handling.

### 3.4 `gpiozero` + `RPi.GPIO` both installed — `install.sh:34`

`gpiozero` is a wrapper over `RPi.GPIO`; on Raspberry Pi 5 / Bookworm, `RPi.GPIO` is deprecated in favour of `lgpio`. Depending on which Pi model is at the bench, GPIO writes may either silently no-op or raise on import. **Confirm the target Pi model and OS** with the bench operator before assuming GPIO 17 will toggle the relay.

### 3.5 Simulated-mode logging is one-shot at import — `ads1115.py:32`

`logger.warning("smbus2 not available — ADS1115 reads will be simulated")` fires once at module import, not on every read. If smbus2 is unexpectedly missing on a real Pi (e.g. apt package update broke it), the daemon will run for hours emitting "simulated" current readings without any operator-visible alert — until a real dry-run fails to be detected. Recommended: emit a `WARNING` log every N minutes when `is_hardware_available()` returns false at read time, OR refuse to start the protection loop with `is_hardware_available() == False` unless an explicit `ALLOW_SIMULATED_ON_PI=1` env is set.

### 3.6 Cloud-side event-type mismatch — **CREATED by the iter-80 contracts PR** that just landed

Daemon emits `current_baseline_learned` (`protection.py:206`); the new `EventType` enum I added in [`Agal/contracts/enums.yaml`](../../Agal/contracts/enums.yaml) uses `baseline_learned` (without the `current_` prefix). Today this is fine because `telemetryIngress.ts:39` documents `current_baseline_learned` as a valid SensorEvent type. But the redesign plan replaces `protection.py` with a `RuleEngine` and the rule-engine event emission should write the new value. Flagging now so the daemon refactor PR fixes both ends in lock-step rather than discovering it at integration time.

### 3.7 ChannelType / SensorKind values on the wire — pre-existing v1 / v2 split

`current_sensor.py:206` emits `"kind": "current"` (legacy closed-enum value). The v2 redesign moves to `kindUri: "agal:reading/current/v1"`. The cloud's `v1ToV2Normalizer` (per the plan) handles this transparently during the 90-day overlap window. No action needed *for this bench-test*.

## 4. Cross-rename consistency — checked

| What | Result |
|---|---|
| `/var/lib/agal-agent` (Docker volume mount) | ✓ `docker-compose.yml:20` |
| `/opt/agal-agent` (install root) | ✓ `install.sh:18`, `systemd/agal-agent.service:7,11` |
| `/etc/agal-agent/config.yaml` (config path) | ✓ `config.py:134`, `main.py:31`, `render_config.py:47`, `entrypoint.sh:7` |
| systemd unit filename `agal-agent.service` | ✓ |
| MQTT topic prefix `agal/{nodeUid}/...` | ✓ `config.py:225–227`, `render_config.py:77–79` |
| MQTT Last-Will-Testament on status topic | ✓ `mqtt_client.py:55–60` (qos=1, retain=true) |
| Required env vars: `NODE_UID`, `AUTH_TOKEN`, `MQTT_BROKER` | ✓ `entrypoint.sh:11–13` (fails loud if absent) |

## 5. Suggested bench script for the next session (when the operator has the kit)

**Both launch-blockers §3.1 and §3.2 were fixed in this audit session** (commits-pending; see §3 markers). The remaining items in §3 (install.sh pip silence, gpiozero/RPi.GPIO dup, one-shot simulated-mode log, daemon-vs-new-EventType mismatch) are not bench-blockers — they're either install-time concerns or post-redesign concerns. You can proceed to the bench procedure below.

For step 3 (baseline learning), the new behavior is: first run learns the baseline as before; subsequent restarts pick up the persisted baseline from `/var/lib/agal-agent/baseline.pump1.current.json` and skip the inrush + learning window entirely. Validate by restarting the daemon mid-run and watching for `loaded persisted baseline: X.XX A` instead of `baseline learned: X.XX A` in the journal.

Procedure once those are clear:

1. **Hardware checklist** from `sensors/README.md` §"Hardware checklist" — voltage divider on ECHO, opto-isolated relay, contactor on live phase, MOV+GDT, PVC stilling tube. **Do NOT skip the voltage divider** — 5 V on a 3.3 V GPIO will brick a pin or worse.
2. **First install**: `sudo bash scripts/install.sh`. **Watch the pip install output** — if it appears to succeed in under 2 seconds, §3.3 has bitten you and zero packages installed. Re-run pip manually with errors visible.
3. **Config**: create `/etc/agal-agent/config.yaml` from the YAML pin config example at `sensors/README.md` §"YAML pin config example". Set `protection.threshold_pct: 20.0` and `confirm_window_s: 5.0`. Set `low_level_threshold_pct: 15` on the ultrasonic sensor.
4. **Start**: `sudo systemctl start agal-agent`. Tail logs: `sudo journalctl -u agal-agent -f`.
5. **Step 1 — Relay closure**. Without the current sensor or pump, send a `setAssetPower {power: on}` command via the cloud (agal1 agri iOS app or `curl` against `setAssetPower`). Verify the relay's NO contacts close with a multimeter (continuity beep). Then `power: off` and verify they open. **Measured: ____ / ____.** (Or, if no app handy: `mosquitto_pub -h <broker> -u <user> -P <pass> -t agal/<nodeUid>/commands -m '{"commandId":"manual-1","type":"setPower","sourceKey":"pump1.relay","value":1}'` and watch the daemon's command_executor log line.)
6. **Step 2 — Current sensor linearity**. Pass 1 A → 3 A → 5 A through the ACS758 (resistive load + clamp ammeter as ground truth). Read the daemon's `pump1.current` telemetry from cloud logs or directly: `python3 -c "from agal_agent.sensors.ads1115 import Ads1115; a = Ads1115(); print([a.read_v(0) for _ in range(50)])"` and convert via `sensitivity_mv_per_a` from your config. **Expect ±2% accuracy after offset calibration.** Measured: 1A → ____ A; 3A → ____ A; 5A → ____ A. Acceptance: each within ±0.1 A of the clamp.
7. **Step 3 — Baseline learning**. Energize the pump (or load) via the relay. Tail logs: expect `pump-start marker recorded; ignoring next 2.0s of readings`, then ~12s later `baseline learned: X.XX A`. **Measured baseline: ____ A.** Acceptance: baseline within ±10% of the clamp reading at running current. **Then `sudo systemctl restart agal-agent`** and energize again; if §3.1 is fixed, the daemon skips the learning window — if not, it learns again from scratch, confirming the bug.
8. **Step 4 — Dry-run cutoff**. With baseline learned, drop the load. Tail logs: expect `XX A < threshold YY A (baseline ZZ A); window-start`, then within `confirm_window_s` (default 5s): `DRY-RUN CUTOFF`. Verify the relay opened with a multimeter. **Measured time to cutoff (window-start → relay open): ____ s.** Acceptance: ≤ `confirm_window_s + 1.0` (covers one poll-interval of slack).
9. **Step 5 — Ultrasonic distance**. Mount JSN-SR04T over a bucket. Fill to 0.5 m, then 2.0 m below the sensor. Read `well1.level` telemetry. **Measured: 0.5m → ____ m; 2.0m → ____ m.** Acceptance: ±5 cm at 0.5 m, ±10 cm at 2 m.
10. **Step 6 — Low-water event**. Lower the bucket level below 15% (set in step 3 config). Tail logs and the cloud `/events` collection. Expect `low_water_level_warning` and **NO** relay change. **Confirmed: event fired = ____ ; relay state unchanged = ____.**

After bench-test, append the measured values back into this file under a new section "§6 — Bench-test results $DATE" and commit.

## 6. Bench-test results

(Not run — pending hardware-equipped session. See §"Scope and disclosure".)

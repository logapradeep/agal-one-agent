# Changelog — agal-one-agent

## 0.2.1 (unreleased, 2026-09-07)

Laptop node for the phone tests (rebuild P4 exit test, `_audit/99`).

- **`blocks.simulated_io: true`** — the block runtime uses in-memory ports
  (`SimulatedIO`) instead of GPIO while the cloud side (MQTT, ingress,
  `getProgram`, acks, variables, alerts) stays real, so a Mac can stand in for
  the reference build while the app is tested on the phone. `main.py` logs a
  warning; never set it on a farm node.
- **`blocks/bench.py`** — `BenchPhysics`: answers the outputs the way the
  plumbing would (valve open + pump on → the plot's flow switch reads flowing
  after 5 s; pump relay on → rated current after 2 s, nominal 4.5 A while the
  rating is 0; three-phase currents and mains-sense inputs present).
- **`blocks/bench_ui.py`** — the bench page: `http://127.0.0.1:<blocks.bench_port>/`
  (default 8765, loopback only) shows the in-memory ports, the program version
  and acknowledgement, the plots with their valves and flow switches, and the
  bench journal; buttons provoke a dry run (current collapses to 30 %), a lost
  phase (three-phase pumps), no flow on an open plot, flow on a closed plot, a
  node outage (runtime stops with safe state, reloads the persisted program,
  boots and resumes — R-14/R-21) and an offline period (no heartbeat, MQTT
  disconnected — R-28). `HeartbeatPublisher.pause()/resume()` added for it.
- **Card variables reach the cloud on every change.** The instance has no MQTT
  consumer for the status topic (Cloud Functions cannot subscribe), so the
  HTTPS ingress is the only path to Firestore and the phone — and the sink
  used it only every 30 s per asset, with only the values that changed in
  the last second. A valve closing between two HTTPS samples stayed "open"
  on the phone (found on the first laptop-node run, 2026-09-08). Now the
  runtime reports a full snapshot of a card's UI / local / node variables
  whenever any of them changes (and every card once after a program loads),
  and `CloudSink` posts them from an ordered, coalescing uploader thread
  (`_VariableUploader`: latest snapshot per asset wins, ≥ 1 s apart per
  asset, failed posts retried) so the runtime never waits on the network.
  MQTT keeps every change as before.
- **Reporting policy:** a card's snapshot goes to the cloud when a bool /
  text value changed, a number moved by ≥ 5 % of its last reported value, or
  30 s passed with anything dirty; `since(...)` timers never trigger a
  report by themselves. Before this every valve rewrote its card every second
  while closed (the phone's asset list kept re-sorting; found 2026-09-08).
- **Ingress budget respected.** The ingress allows 1000 requests per node
  per hour; the first laptop-node run spent it in minutes and every report
  (heartbeats included) got 429 for the rest of the hour. Card snapshots now
  draw from a token bucket of 600/h (burst 20), timer-only changes never
  cost a report, and a 429 pauses snapshots for 60 s; `HttpReporter` keeps
  `last_status`.
- **Bench page never takes the agent down:** a held port falls back to a free
  one, and any start-up failure is logged instead of raised.
- Version constant bumped to 0.2.1 (`setup.py`, `__init__`).
- Tests: 8 new (224 total). See `LAPTOP_NODE.md`.

## 0.2.0 (2026-09-07)

Automation-block runtime — ADR-017 (contracts v1.5.0); rebuild phase P1 of
`_audit/99-rebuild-plan.md`. The node now executes the program the cloud
compiles for it (a Node Automation Block plus every asset's Asset Automation
Block) and is the single writer of every output; the hard-coded protection
thread is no longer started when a program is loaded.

- **`agal_one_agent/blocks/`** — `expr` (Python port of the reference expression
  parser; passes the shared golden vectors), `runtime` (`BlockRuntime`: compile
  with full semantic validation, 1 s tick + input-driven passes, `when` /
  `sequence` / `limit` / `on` rules, schedules with sunrise/sunset, plots and the
  pump interlock, baselines, interruptions, persistence, acknowledgement,
  safe-state on stop), `io` (`SimulatedIO`, `HardwareIO`), `clock`
  (`SystemClock` with NTP/RTC probing, `SimClock`), `sun`, `sync`
  (`ProgramStore`, `ProgramSync`: `getProgram` pull → compile → persist →
  `programAck`), `cloud` (`CloudSink` fan-out, capabilities, heartbeat extras),
  `simulate` (`agal-one-agent-sim` CLI — the simulated bench).
- **Wire (telemetryIngress v1.5.0):** `programAck`, `variables`, `alert`
  messages over HTTPS (`HttpReporter`) and on the MQTT status topic
  (`AgalOneMqttClient`); `getProgram` pull; heartbeat `status` carries
  `programVersion`, `programStatus`, `capabilities`.
- **Commands:** `syncProgram {version}` (pull + apply + ack), `runPlot`,
  `stopPlot`, `setVariable`; `setPower` / `setPortValue` on a port the program
  owns are routed through the runtime (interruption semantics, R-23).
- **Config:** `blocks: {enabled, state_dir, legacy_protection, tick_seconds}`;
  `agal-one-agent --offline` runs the persisted program without the cloud.
- **Tests:** 82 new (expression vectors, simulated-bench scenarios for R-7,
  R-8, R-10 to R-16, R-19 to R-23, R-25, sync/ack, envelopes); 207 total.
- Version constants unified on package metadata (`__version__`).

## 0.1.8 (unreleased, 2026-07-07)

ADR-013 P0 (telemetry durability) + ADR-011 v1 (raw-LoRa, DARK).

- **Durable telemetry ring buffer (ADR-013 §5, P0):** new
  `agal_one_agent/telemetry_buffer.py` — a SQLite WAL ring buffer at
  `/var/lib/agal-one-agent/telemetry.db` (stdlib `sqlite3`, no new dep). Every
  sampling cycle appends readings (ts + monotonic seq + boot-session id)
  **before** the live publish, so a network outage becomes a delay, not a hole
  in *paid* history. Ring bounds: keeps ≥48 h (configurable), hard caps
  7 days / 64 MB, drop-oldest with a one-time `buffer_overflow` sensor_event on
  first drop. WAL + `synchronous=NORMAL` = one fsync per batch commit (SD-card
  wear).
- **Batch uploader (ADR-013 §5.2/§5.3):** new
  `agal_one_agent/telemetry_uploader.py` — a background thread drains the buffer
  to the ingress in `telemetry_batch` payloads (≤500 readings / ≤256 KB, oldest-
  first within tier priority — raw drains before basic), with exponential
  backoff + jitter (1 s → 5 min cap) and on-success prune. Rows are marked sent
  only on HTTP 200. Woken on MQTT reconnect to flush an outage backlog.
  `HttpReporter.report_telemetry_batch()` emits the `telemetry_batch` envelope
  and rides the existing `Authorization: Bearer` header (v0.1.7); `_post()` now
  returns success so the uploader prunes only on confirmed delivery.
- **Adaptive live cadence (ADR-013 §9-D3, ratified):** new
  `agal_one_agent/live_cadence.py` — publishes the live snapshot every 10 s
  while an app is watching, else 60 s (config `telemetry.live_idle_seconds`).
  The watch signal arrives via a new `liveWatch` MQTT command (backend forwards
  a presence/onSnapshot heartbeat; a config default covers bench/dev); a watch
  is held for a TTL then decays back to idle so a closed app relaxes the node
  automatically. `TelemetryPublisher` re-evaluates the interval each cycle.
- **Clock discipline (ADR-013 §5.4):** buffered readings carry `ts_uncertain`
  when the wall clock is pre-NTP; `TelemetryBuffer.heal_uncertain_clock()`
  re-bases them from the observed correction once time is trusted; whatever
  stays uncertain uploads with `tsUncertain: true` for the server to stamp
  receive-time.
- **Raw-LoRa star listener — DARK (ADR-011 v1, §6/§10):** new
  `agal_one_agent/lora_listener.py` — an SX127x (SX1276/SX1278) SPI reader
  scaffold. **Inert by default:** `spidev` is import-guarded (like the BNO055
  driver) and the listener only goes live when `lora.mode == "raw_star"` AND
  `lora.enabled` AND spidev is importable — none true on any current node.
  Implements (testable without hardware): compact leaf-frame header decode
  (ver+type · childShortId · seq · encrypted CBOR body · MIC), seq dedupe with a
  16-wide reorder window, 60 s pairing window, `lora_survey_sample` emission for
  the placement signal meter, `lora_child_status` (battery/rssi/snr/quality),
  the §10.3 quality-word thresholds (margin formula, SF-independent) and §10.4
  tri-state link-state freshness. LoRa readings enter the **same** buffer + live
  path as wired children, sourceKey `lora.<childShortId>.<portKey>`. The
  register-level SX127x RX driver and the AES-128-CCM body decode are documented
  TODOs gated on the P0 bench spike (ADR-011 R1); the AES-128 per-farm key is a
  documented placeholder (`FARM_KEY_PLACEHOLDER`; `K_farm` lives in Secret
  Manager, only `farmKeyId` on the node doc). New MQTT publishers
  `publish_lora_survey_sample` / `publish_lora_child_status` and a
  `loraPairingWindow` command.
- **Config (ADR-013 §5.7 / ADR-011 §6):** `telemetry.batch{flush_interval_sec,
  max_readings, buffer_max_mb, retention_hours}`, `telemetry.live_watch_default`,
  `telemetry.live_idle_seconds`, `telemetry.buffer_db_path`; `lora.{mode,
  enabled, region, farm_key_id, radio{...}}` for the raw-star block +
  `is_lora_raw_star` property. All additive with safe defaults; existing
  configs parse unchanged and stay live-only + LoRa-dark.
- **setup.py:** bumped to **0.1.8**; `extras_require["rpi"]` gains
  `cryptography>=42.0` (for the future LoRa AES-CCM path; import-guarded/unused
  today). `spidev` now also serves the LoRa listener. Core deps unchanged.
- **Tests:** +67 (58 → **125 passing**) — ring buffer append/drain/prune/
  overflow/clock-heal/priority (`test_telemetry_buffer.py`), uploader
  drain/backoff/outage-recovery (`test_telemetry_uploader.py`), LoRa frame
  decoder/dedupe/survey/quality-word/link-state/dark-gate
  (`test_lora_listener.py`), cadence switch/TTL (`test_live_cadence.py`), config
  parsing (`test_config_p0_lora.py`), and P0 wire-shape + buffer-first
  integration (`test_p0_wire_integration.py`).

> **v0.1.8 OTA still founder-gated:** shipping this to nodes needs the
> rename-finalize (the uncommitted `agal_agent → agal_one_agent` WIP + the
> hand-applied v0.1.6 port both still sit in the working tree — see the 0.1.7
> note below) followed by a `v0.1.8` tag. This P0/LoRa work was added on top of
> that working tree and mirrors how the v0.1.6 port was handled. Coordinate the
> ingress side: `telemetryIngress` must gain the `telemetry_batch` handler +
> authToken check and the `lora_survey_sample` / `lora_child_status` intake
> (owned by the backend agent this wave) before the buffer's batches land
> anywhere — until then a v0.1.8 node still publishes the unchanged live channel
> and simply retains batches in its buffer (no data loss, no server 200 → no
> prune).

## 0.1.7 (unreleased, 2026-07-06)

- **Telemetry ingress authentication (ADR-013 P0.5):** `HttpReporter` now
  sends `Authorization: Bearer <auth_token>` (the node's config.yaml
  authToken — same credential as the MQTT password) on every HTTP report:
  telemetry, status, commandAck, firmware events, and boot-state fetch.
  The backend `telemetryIngress` currently accepts tokenless requests with a
  logged warning (`TELEMETRY_AUTH_ENFORCE=false`) so <= v0.1.6 daemons keep
  working; enforcement flips server-side once the fleet is on >= v0.1.7.
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

- **v0.1.6 provisioning fixes PORTED into this branch (2026-07-06):** a plain
  `git merge v0.1.6` was not possible — the working tree carries the large
  uncommitted `agal_agent → agal_one_agent` rename, so the three v0.1.6 code
  hunks were applied by hand onto the renamed files instead:
  `TelemetryConfig.ingress_url` (config-driven, parsed from
  `telemetry.ingress_url`); `HttpReporter(base_url="")` now resolves
  `base_url or TELEMETRY_INGRESS_URL`; `main.py` passes
  `config.telemetry.ingress_url`. The module-default URLs were also moved off
  the retired us-central1 hosts — **both** `TELEMETRY_INGRESS_URL` **and**
  `BOOT_STATE_URL` now point at `asia-south1-agal-one-prod.cloudfunctions.net`
  (the latter closes the separately-known dead-`getBootState` `-uc` URL bug).
  All 58 tests pass with the port + BNO055 + auth-header changes coexisting.
  NOTE: applied in the working tree but left UNCOMMITTED — it belongs in the
  founder's rename-finalization commit; commit the rename + this port together
  before tagging v0.1.7 for OTA. `getBootState`'s asia-south1 deployment should
  be curl-verified before the OTA (URL format is the canonical gen2 alias, but
  the endpoint's live presence in that region was not verified here).

### Version-number reconciliation note

`v0.1.6` ("config-driven telemetry ingress URL + agal-one-agent entry point",
commit `25cd7f3`, tagged on origin/main and deployed to nodes 2026-07-05) was
released from the **pre-rename `menvayal_agent` lineage**. Its changes are now
ported into this `refactor/rename-to-agal-one-agent` branch (see the entry
above). To avoid colliding with the released tag, this branch jumps straight
to `0.1.7`.

## 0.1.6 (2026-07-05, released from origin/main — not in this branch yet)

- Config-driven telemetry ingress URL; `agal-one-agent` console-script alias.

## 0.1.5 and earlier

See git tags `v0.1.0` … `v0.1.5` (commit messages carry the summaries).

# Changelog — agal-one-agent

## 0.3.3 (unreleased)

A new version rather than more of 0.3.2: 0.3.2 (cb03e4b) is already on `main` as its own
build, and the OTA path tells builds apart by their version string. Neither is tagged;
`/config/agent` still pins 0.3.1.

**Fix — a program reload switched every running output off.** The cloud recompiles the whole
node program on every block, card or link write and the node reloads it. `compile` rebuilt
every variable at its default while the output flush compared against what it had last
written, so the first pass after any reload during a plot run turned the pump and its valves
OFF; a running schedule also lost its end time and never ended (wave 0, G1 —
`scratchpad/understand/reload_sim.py`). A program loaded while another one runs now takes
over from it:

- **outputs keep their level**: a port variable is carried by its port's stable key (assetId +
  sourceKey + direction), whatever else its declaration says. Nothing is written for an output
  that did not change — an identical bundle reload writes nothing at all;
- **variables** whose declaration is unchanged keep their value and their `since()`; a changed
  one starts at its new default. NAB variables are matched by name;
- **rules**: an unchanged rule keeps its hold, fired, ignore-window, limit and sequence state;
  a new or changed rule starts afresh, as at load, and only that rule. Baselines being learnt
  go on learning;
- **runs and schedules** carry on: a running schedule ends at its planned time, a timed plot
  run at its own. Plots taken out of the program while they run — one or several in the same
  reload — end their runs and close their valves (pump first), found through the program the
  run was started in: when a field is deleted its valve variables leave the NAB with it while
  the valve cards stay on the node, and those cards are switched off;
- **a running schedule taken out still ends when it would have**: its end actions (or the
  default `stop_plot` of its plot) run at its planned end, each on what it named when it was
  written (a NAB variable is resolved to its card variable, so a renamed or removed NAB
  variable still reaches the valve), with `schedule_ended`. A running schedule whose end
  actions a reload changed runs the end it was started with too, so what its start switched on
  is switched off. A schedule taken out and put back before its end is the same run. An
  orphaned end only ends: it never starts a plot or a sequence the schedule would have chained to;
- **only an output the new program no longer has is let go of**, on the next pass, in the R-7
  order (motors before valves): switched off and the line freed — whatever its safe state.
  `safeState` (R-14) is what a pump does when the node's software stops or its power goes; an
  output the program no longer has is supervised by no rule (no maximum run, no dry-run
  protection) and on a port-table node not even its own card can switch it, so it never stays
  on. An output re-stamped onto another line carries its level there and the old line is
  switched off and freed; a changed wiring fact (active low) rewrites the line once. A line the
  new program uses again — another card drives it, or it is now read as an input — is not
  touched (an input takes the line over when it is sampled), and a release planned by an earlier
  reload that no pass has applied yet is dropped when its line is back in the program. Each
  release is an `output_released` event. A clean stop lets go of a release no pass has applied yet;
- the swap holds the runtime lock, which a pass holds from start to end, so it always lands
  between two passes, and it is **atomic**: if taking over fails half-way, every piece of
  runtime state is put back as it was, nothing is announced, and the bundle is rejected
  (`could not take over from the running program: …`); `ProgramSync` acks any compile failure
  as rejected instead of losing the pull thread. A program loaded **cold** — the first one, or
  after `stop()` (the bench's outage button) — still starts from its defaults and the persisted
  state, as after a restart; the persisted state is read only then, so a reload no longer
  re-announces a resumed run, and a state file that cannot be used never keeps a program out.

`IOAdapter.release_output` is new (LineIO frees the line, a PWM channel is stopped, a
Broadcom-numbered line stays an output at the level just written). 30 tests in
`tests/test_blocks_reload.py`, the reload simulation among them.

Review of the reload (wave 0): two running plots taken out in one reload crashed the take-over
half-way (the program swapped in, never acked, outputs left on, and with timed runs every later
pass failed before writing an output); a running schedule taken out never ran its end; a plot
taken out with its valve variable left the valve open; a release planned by one reload could
switch off a line the next reload gave to another card, or drive a line that had become an
input; a `last_state` output left the program running and could no longer be switched off.
Each is fixed above and has its test; the 14 new ones fail without the fix (on 71a2b7e).

**Security — a switch command moves a port only for the card it is linked to (RBAC-01, agent
side).** A card document names its node and its ports itself, so a stranger's card could name
somebody else's node and GPIO and switch it through `setAssetPower`; on the node, a program
that did not know the command's `assetId` even fell back to *every* card's `device.power`
and drove another card's program-owned output. What the node knows of its port table is what
the cloud stamped into the running program: every port a card's block reads or drives, with
that card (`BlockRuntime.port_links()`). `ports/links.py`:

- a node **runs a port table** when the cloud reported a `portsVersion` above 0 (the
  `reportHardware` answer or a `syncPorts`) or its program names a port by chip label + line.
  There, `setPower` / `setPortValue` are carried out only for a port linked to the card the
  command names, always through the runtime; anything else — no card, a card the port is not
  linked to, an input — is refused with a failed `commandAck` in words (`refused: device.power
  of card … is not linked to a port of this node`), and the legacy GPIO executor is never
  reached. With a table and no program yet, every switch is refused;
- `testPort` naming a card is refused unless the port is linked to that card (`refused: DO1 is
  linked to another card on this node`), reported as a failed port test and a failed ack. A
  free port is tested as before (the first check on a new board); a linked port tested with no
  card named runs as the node-level check the cloud authorised, and is logged;
- a node **without a port table** keeps the legacy GPIO path, now logged (`legacy GPIO path:
  setPower for card …, GPIO … (this node has no port table)`), except onto a GPIO the running
  program drives for another card — that is refused (a second writer);
- the runtime itself answers only for the card a command names (`_port_var` no longer falls
  back to every card);
- **boot reconciliation** (`getBootState` lists every card that names this node, and applied
  them through the legacy executor) goes through the same check and never writes a port the
  running program drives: the program decides after a restart (R-21);
- **one port, one card**: a bundle in which two cards (or two ports of one card) name the same
  physical line and one of them drives it is refused — `line 17 of pinctrl-bcm2835 is claimed by
  device.power of card intruder-1 and device.power of card pump-1: a port is linked to one card` —
  and the running program stays. A `gpio` and a `pwm` port on one GPIO number are one line; two
  cards reading one input are allowed. Before, the node ran both, and a stranger's card with a
  default of ON drove the line on the next pass with no command at all;
- **the port table is remembered**: the highest `portsVersion` the node heard is kept in its
  state directory (`ports_version.json`), so a node that once ran a port table keeps running one
  after a restart whose hardware report is not answered. The report is retried with back-off
  (10 s doubling to 10 min) until it is answered. Until the node knows (an answer or the kept
  value), the legacy GPIO path is refused (`refused: this node does not know yet whether it
  runs a port table …`) — a card's own output in the running program still goes through the
  runtime — and boot reconciliation waits up to 30 s for the answer first;
- **without a port table**, a GPIO the running program reads (a flow switch the dry-run rules
  watch) is refused like one it drives (`refused: GPIO 5 is read by the running program`), and a
  switch naming a card's own input is refused; before, it was set up as a driven output;
- the check and the dispatch of a switch hold the runtime lock, so a program pulled on another
  thread cannot land between them, and a node that runs a port table never hands a switch to
  the legacy executor, whatever let it past the check.

**What this does not cover — the backend half of RBAC-01.** The node checks what the cloud
stamped into the program it runs, and today the backend copies each card's own
`portBindings[].transport` into the bundle; a card owner writes their own bindings and
`nodeUid`, and `writeAssetProgram` recompiles the node a card names. Refusing a second card on a
line closes the double claim, not a stranger's card on a line no other card has linked yet, and
a chip label + line and a Broadcom number naming the same pin are not related on the node
(nothing here names a board). These hold only once the backend stamps transports from the
node's port table and checks the card's link.

26 tests in `tests/test_port_links.py`; `main()` was also driven offline with fake MQTT and
HTTP: a stranger's `setPower` and a `testPort` naming the wrong card were refused with their
reasons, the linked card's `setPower` completed through the runtime; after the review, a
hardware report answered on the third try was kept as `portsVersion` 2 and survived a restart
with no answer, a bundle adding a second card on line 17 was acked rejected in those words, and
on a node with nothing kept and no answer the legacy path was refused while the program's own
pump still switched.

Tests read the contracts from `AGAL_CONTRACTS_DIR` when it is set (a worktree or a lone clone
of the agent); the default stays `../../../contracts`.

## 0.3.2 (unreleased)

**Fix — an active-low output was switched ON for a moment whenever its line was opened.**
Low-cost opto-isolated relay boards (HW-281 class, JQC3F relays) close a relay when their
input is LOW, and a port says so with `activeLow`. `LineIO` requested every output line
with python-periphery's `"out"`, which the kernel starts LOW (`gpio_cdev1` / `gpio_cdev2`
`_reopen`), and only then wrote the inverted level. So the first write after start-up, the
safe-state write on a clean stop, and the first write after a port test let go of a line all
pulsed the relay ON before switching it OFF, long enough to tick a contactor or a valve.

- **`ports/gpiochip.py`**: an output line is requested AT the level it is about to be given
  (`"high"` / `"low"`), never `"out"`. Nothing else changes: one request per line,
  `PortUnavailable` in words, the inversion applied at the line.
- **`handlers/gpio_handler.py`** (ports still named by Broadcom number): the same, through
  `GPIO.setup(…, initial=)`, instead of whatever the pin latch held.
- 6 new tests, with a fake that starts a line where python-periphery does: no LOW reaches an
  active-low line that is OFF, whether on the first write, after a release, after a read,
  after a restart, on the runtime's first pass, or on a clean stop. The pulse of `testPort`
  is the only LOW (286 tests).

On the board: every relay output port of such a board has `activeLow: true`, and
`/boot/firmware/config.txt` keeps the outputs HIGH from power-on until the agent opens them
(`gpio=17,27,22,23,24,25,5,6=op,dh` on the pump-house panel's Pi 3B).

## 0.3.1 (2026-09-20)

An analog input is read from what its PORT says (contracts v1.9.1).

- **`ports/analog.py`** — an AI port is an ADC channel plus two facts that travel with its
  transport (in the compiled bundle and in a `testPort` command): `measure`
  `{mode: dc | ac_rms, windowMs}` and `transform` `{scale = units per volt, offset,
  clampMin, clampMax}`. A current sensor on a mains motor is therefore **not a driver** and
  needs no `config.pins` entry: it is `ac_rms` and a scale on an ADS1115 channel. `ac_rms`
  removes the window's mean before it squares, so a Hall sensor idling at half its supply
  or a current transformer on a mid-rail bias needs no zero calibration, and a drifting
  supply does not read as current. One driver today: `ads1115` on I2C, at its widest range
  (a 5 V sensor idles at 2.5 V). A channel is sampled at most twice a second, so three
  phase currents do not stretch a program pass. Nothing is simulated: a port the agent
  cannot read answers `None` and a test says so in words.
- **The running program** reads such a port through `HardwareIO` (the value arrives in the
  port's unit — amps, not volts); the older `config.pins` sensor drivers keep working.
- **`testPort` `read` on an AI port** watches it for a few seconds and answers in the
  port's unit with the volts behind it: `11.8 A now; 11.6–12.0 A over 5 s (AC RMS,
  0.472 V at the channel)`. An ADC that does not answer fails the test with its address.
- **Fix — a GPIO chip was reported twice.** Raspberry Pi OS keeps `/dev/gpiochip4` as a
  link to `gpiochip0`; chips are now listed once, by their real device (seen on the first
  real board, a Pi 3 Model B, 2026-09-20).

## 0.3.0 (2026-09-20)

Node linking (ADR-024, contracts v1.9.0 — `Agal/contracts/nodes/README.md`): a board is
data in the cloud, and the agent does the same three things on every Linux board. Nothing
in the agent names a board.

- **`ports/gpiochip.py`** — GPIO through the kernel **character device**, addressed by the
  chip's **label** and a line offset (`pinctrl-bcm2835` line 17), never by chip number
  (it differs between OS images) or Broadcom pin number (it exists on one board family).
  Chip and line information — including **which lines the kernel holds** — comes from two
  ioctls (v2 ABI, falling back to v1); line I/O from `python-periphery`. All pure Python:
  nothing compiles on any board or architecture. `LineIO` keeps one request per line.
- **`ports/inventory.py`** — the **hardware report**, posted to `reportHardware` at start
  and after every `syncPorts`: what the board is (model from the device tree, serial, SoC,
  OS, kernel, memory, Python, agent version, the interface of the default route) and what
  it has (GPIO chips with kernel-held lines, I2C / SPI / serial devices with the stable
  `/dev/serial/by-id` path of a USB adapter, RTC, IIO ADCs, the I2C addresses that answer).
  The cloud checks a port table against THIS, not against a list of boards.
- **`testPort`** (`ports/port_test.py`) — the commissioning check of one port: pulse an
  output (at most 5 s; off and released even when interrupted; active-low honoured) or
  watch an input. The command carries the port's own transport, so it works before any
  card or program exists — the first check on a new board is an LED on `DIO1`. A port
  the running program drives is pulsed **through the runtime**: there is one writer.
- **`scanBus`** (`ports/bus_scan.py`) — what answers on I2C, or on a short range of
  Modbus units over RS-485 (one read-coils request each; a reply **or an exception**
  proves a device is there). A scan only suggests; the app asks the person what it is.
- **`syncPorts`** — the port table changed: pull the program, report the hardware again.
- **Block runtime I/O** — `HardwareIO` drives ports that name a chip label + line through
  `LineIO`; a bundle naming a chip this board does not have is rejected at compile time.
  Ports with a Broadcom number keep working through the old handlers.
- Not yet: the generic Modbus RTU driver for relay boxes and meters, the MCP23017 /
  PCF8574 expander drivers, per-port health reports, and the v2 block runtime
  (`capabilities.blockLanguage` stays absent = 1).
- Tests: `tests/test_ports_node_linking.py` (19, all against fakes); suite 263.

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
- **Three-phase bench (`--kit farm3`, 2026-09-20):** `build_bundle_from_defaults` composes the
  first three-phase site — a three-phase pump with every port of the maximal v2 template
  linked (relay, three currents, three mains-sense, three voltages) and four solenoid valves
  without flow switches, the NAB rebuilt for four plots the way the app's builder does.
  `tests/test_blocks_three_phase.py` (17): phase loss, start refused on a missing phase,
  imbalance, dry run, overload (inert until a rated current is set), voltage range and start
  refused on bad voltage, did-not-start reported as such and not as a dry run, a run started
  at the starter panel, trip flags cleared on the next start, and the NAB's response — any
  pump fault closes the valves and raises one critical alert; a hand-start with every valve
  closed is critical. The three-phase template had no bench coverage before. 244 tests.
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

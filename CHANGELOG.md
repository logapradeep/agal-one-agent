# Changelog — agal-one-agent

## 0.4.4 (2026-10-23)

**A single-pulse valve closed at start-up and before a shutdown, an update that waits says so, a valve's words name its
port, the node's clock (contracts v1.24.0; programs/README.md §7.9, nodes/README.md §13–§14; SYNC.md v1.24.0).** Not
tagged, not released: releasing it — a tag and `/config/agent` 0.4.4 — is the founder's, after the backend deploy. It
reaches the Pi over the air (the app's Update button); nothing is done on its terminal.

- **A single-pulse valve is closed at start-up and before a shutdown — built in** (`_audit/107` follow-up 28; the
  founder's rule of 2026-10-14). `RuntimeV3.apply_safe_state` — a clean stop, the `finally` of main() after SIGTERM
  (systemd's stop or restart, an update's restart, a reboot) — now sends, after the motors and the continuous valves are
  off, ONE close pulse to every single-pulse valve that is not in FAULT, in `assets[]` order, whatever position it
  recorded: the pulse mark on disk first, the position after (as every pulse), its supply read before and after (not
  present: FAULT — `supply_lost` and no pulse, or `pulse_cut_short`), `valve_closed` (cause `shutdown`); only then does
  `stop()` record the clean stop, and it hands the stop's events to the sink (no pass runs after it). At a clean start
  (a clean-stop record, no pulse in progress, the same configuration, not in FAULT) the cold load marks each valve
  `start_close`, and the boot pass's flush sends it one close pulse with the switch-offs, before any open pulse, whatever
  position it recorded — `valve_closed` (cause `start_up`); a valve its boot pass found with no supply is in FAULT and gets
  none; what a block set for it in that pass waits for the next pass; a run the restart interrupted re-opens it 30 s
  later. Before, a clean restart kept the recorded position (a valve recorded open got a close pulse at the boot pass,
  one recorded closed got none) and nothing closed it before a stop. An unclean start is unchanged: FAULT, no pulse. A
  reload and the switch of language (`stop(safe_state=False)`, `take_over_language`) send none.
- **A valve's configuration and its port ids.** `valve.same_configuration` compares two valve entries leaving out a
  port id stated on one side only — the first bundle that names them (contracts v1.24.0), or a state file 0.4.3 wrote, is
  not a changed configuration (no `position_unknown` at the update to 0.4.4); a coil moved to another port is one.
- **The words name the port** (SF-5, the founder's answer 7). The bundle's valve entry now names its ports
  (`openPortId`, `closePortId`, `supplyPortId` — words only), and the node says: *refused: Valve 2's supply on DI3 (UPS
  supply) is not present* (reset refusals), *the supply on DI3 (UPS supply) dropped* (`valve_fault` detail), *the close
  pulse on DO4 could not be sent — Valve 2 stays in fault*, *the open coil of Valve 2 on DO3 could not be switched off: no
  close pulse sent — …*, *refused: DO3 of Valve 2 is driven by its valve driver — switch valve.open*, and in a compile
  refusal *DO4 of Valve 2 (valve.close_coil) states no direction …* / *valve Valve 2 (asset_x): coil DO4
  ('valve.close_coil') is not an output port of the card*. A bundle without port ids gets 0.4.3's words.
- **An update that waits says so** (`_audit/107` follow-up 32; the Pump house's update of 2026-10-23 waited 7 minutes for
  Mtr1 and the apps said only *Update under way*). `ota_updater._wait_until_idle` keeps a record of its wait —
  `wait_report()`, the heartbeat's `otaWait`: phase `waiting_idle` with the cards that are on (`outputs_report_for`: the
  program's `outputs_on_items` — each card once by its name in the bundle — and the legacy outputs by their config names),
  the runs in progress, `since`, `until` (= since + BUSY_WAIT_S) and `at`; phase `busy` with what stayed on and `endedAt`
  when the 30 minutes run out, kept until the next update starts. Both waits (before the download, and before the restart
  when something came on while pip ran). One heartbeat is sent at once (`HeartbeatPublisher.nudge`, on a thread of its
  own; one timer at a time) when the wait begins, when what is on changes (at most every 5 s) and when it ends busy. The
  dates are the node's trusted time (the backend's, from its HTTPS answers).
- **The node's clock** (ADR-031 §6, accepted 2026-10-23: each node reports its clock source and offset). NEW
  `clock_care.py`: the `clock-care` thread reads, every 60 s, `timedatectl show` (NTPSynchronized), `systemctl
  is-active` of systemd-timesyncd, chrony, chronyd, ntpsec, ntp, ntpd and /dev/rtc0 — argv lists, 3 s bounds,
  `net_care.run_tool` (killed with its process group; none started while one that will not die lives) — into `source`
  (ntp | rtc | none | unknown), `service` and `rtc`. `ServerTime` takes a sample of the wall clock against every HTTPS
  answer's Date (`add_sample`: + 500 ms, the history restarted when the wall clock steps by more than 1 s against the
  monotonic clock) and `offset()` is the median of the last 15 (`offset_of`). Every online heartbeat carries `clock`
  {source, service, rtc, offsetMs, offsetSamples, offsetAt, at}, from memory.
- Version 0.4.4 (`setup.py`, `__init__`). Tests: NEW `tests/test_v3_single_pulse.py` (29: each start and stop path, the
  order of the shutdown record, the supply, FAULT kept, the port-id configuration rule, the words), `tests/test_ota_wait.py`
  (10), `tests/test_clock_care.py` (16, the contract's `clock.json` vectors); updated for the new behaviour:
  `test_v3_valves.py`, `test_v3_corners.py`, `test_v3_reset_ack.py` (+ the old words on a bundle without port ids),
  `test_v3_events_schema.py` (both causes of `valve_closed` validated against event.schema.json),
  `test_ota_rollback.py` (the new keyword arguments), `test_v1_parity.py` (`clock` checked and taken out, as `network`).

## 0.4.3 (2026-10-22)

**Why the cloud could not be reached (contracts v1.18.0, nodes/README.md §12; SYNC.md v1.18.0).** Not tagged, not
released: releasing it — a tag and `/config/agent` 0.4.3 — is the founder's, after the backend deploy. It reaches the Pi
over the air (the app's Update button); nothing is done on its terminal.

**Why (2026-10-22, the Pump house).** From 16:18Z the Pi (a Raspberry Pi 3B, Raspberry Pi OS on Debian trixie,
NetworkManager, agent 0.4.2) lost the cloud about once a minute for 25+ minutes: its MQTT connection reconnected every
~61–92 s and most HTTPS heartbeats between got no answer. Every `connectivity_gap` said its Wi-Fi stayed connected
(−42 … −44 dBm, power saving off), so the fault was beyond the Wi-Fi — but nothing said whether the router stopped
answering, the internet behind it was down, the name lookup (DNS) failed, or the cloud itself did not answer.

- **Every request that got no answer is classified.** `HttpReporter._open` (the one call every request makes) hands the
  `LinkWatch` the kind — `net_care.failure_kind`, from the error's TYPE: `dns` (a `socket.gaierror`), `connect` (refused,
  the network or host unreachable, a connection that timed out), `tls` (an `ssl.SSLError` while connecting, or CPython's
  *The handshake operation timed out*), `timeout` (a `TimeoutError` while waiting for the answer — urllib does not wrap
  it), `other`. The error's text stays in the node's log; nothing of it is sent. An HTTP answer of any status is still an
  answer.
- **NEW `NetworkProbe`, on a thread of its own (`net-probe`)** — never the runtime's, never under its lock
  (programs/README.md §4.8). Woken (an Event, set by the request's thread, which never waits) when a silence begins
  after an answer, it probes while the silence lasts, one probe at a time, a round at most every 10 s and at most 12
  rounds per silence: the ROUTER — `ip -4 route show default` (the lowest metric; a `linkdown` route only when no other),
  `ping -n -c1 -W1 <gateway>`, then `ip neigh show <gateway>` (read after the ping, which makes the kernel resolve the
  router): answered (the ping, or the entry REACHABLE), silent (no ping answer and the entry FAILED / INCOMPLETE),
  unknown; when the router is silent the round ends there; the INTERNET — a TCP connection, 3 s, to the cloud's
  last-known address on 443, closed at once (no TLS, no request; never a third-party host); the NAME LOOKUP —
  `getent ahosts <host>`, 3 s (no answer in 3 s is failed). The last-known address is the first IPv4 address of the last
  successful lookup — a round's, or the probe thread's own outside a silence: once after the cloud first answers, then at
  most once an hour (the only program the probes run outside a silence). Every program is an argv list, never a shell,
  with `LC_ALL=C` and a bound (`ip` 2 s, `ping` 3 s, `getent` 3 s), killed with its process group when it does not end;
  one that will not die is left and no program is started while it lives. A round hands each finding to the watch as
  soon as it is known, and only for the silence that lasts (a round that ends after the answer is dropped). Nothing is
  probed before the cloud has answered once, nor on a board without /sys/class/net (the probe thread starts with the
  net-care thread and stops with it).
- **The gap says why.** `connectivity_gap`'s payload.connectivity gains `failures` {dns, connect, tls, timeout, other},
  `router`, `internet`, `nameLookup` (the worse finding of the silence's rounds) and `cause` — the decision table
  (`net_care.cause_of`, the reference's `causeOf`): wifi (the Wi-Fi dropped) · router (silent, the Wi-Fi up throughout) ·
  internet (the router answered, the cloud's address unreachable) · dns (the address reached, the name not looked up —
  in a probe, or by every request) · cloud (everything the node could check worked) · unknown. The log says it in words
  (*no answer from the cloud for 5 min 10 s (…); its router answered, but the internet behind it was down — reported as
  connectivity_gap (router answered, internet unreachable, name lookup failed; no answer: 9 connect)*).
- `wire_network` (main.py's call, unchanged) also starts the probe thread, with the reporter's ingress host as its cloud;
  `NetworkCare.stop()` stops it. Version 0.4.3 (`setup.py`, `__init__`).
- Tests: NEW `tests/test_net_why.py` (133): every error urllib raises → its kind, constructed and REAL (a name that does
  not resolve, a refused port, a server that speaks no TLS, one that never finishes the handshake, an HTTP server that
  never answers, one that closes) through the real reporter; every new vector of `nodes/vectors/network.json` (`ip -4
  route`, `ip neigh`, iputils `ping`'s exits, glibc `getent ahosts`, each finding, merge, the decision table); the
  round's exact argv lists and bounds; the router silent ends the round; the address lookup (after the first answer,
  hourly, a round's refreshing it, IPv4 first, kept when DNS fails), no third-party host; rounds ≥ 10 s apart and 12 at
  most per silence; a round of an ended silence dropped; a probe that will not die starts no other program; a ping
  that does not end killed at its bound (the real run_tool); no probe possible → unknown; nothing before the first
  answer; on a real ProgramHost the probes run on `net-probe` only and a pass is never delayed (hung ip, ping and getent);
  end to end the real reporter, watch, probe thread and node-event queue against a local ingress — the gap posted once
  saying `internet`, the error's words not in it; `wire_network` starts and stops the probe; every gap of every cause
  validates (AJV). `tests/test_net_care.py`: the 0.4.2 gap vectors on their v1.16.0 fields (the five checked present and
  the cause the table's), the `diagnose` ones whole, the Pump house's gaps with the five. `tests/test_v1_parity.py`: the
  version is 0.4.3; nothing it records changes (no probe runs on a Mac).

## 0.4.2 (2026-10-18)

**The node's Wi-Fi care, its network report and its gaps (contracts v1.16.0, nodes/README.md §11; SYNC.md v1.16.0).**
Not tagged, not released: releasing it — a tag and `/config/agent` 0.4.2 — is the founder's, after the backend deploy.
It reaches the Pi over the air (the app's Update button); nothing is done on its terminal.

**Why (2026-10-18, the Pump house).** The Pi (a Raspberry Pi 3B, Raspberry Pi OS on Debian trixie, NetworkManager, agent
0.4.1) went silent for about five minutes, 10:40:13Z → 10:45:23Z, without restarting (`uptimeSeconds` ran on): no
request reached the cloud, so it lost its network. A Pi 3B's brcmfmac Wi-Fi power saving is a common cause of short
drops, but the node reported nothing about its Wi-Fi, so it could not be told from a router or internet drop.

- **NEW `net_care.py`, on a thread of its own (`net-care`)** — never the runtime's, never under its lock (programs/README.md
  §4.8). Every 5 s it reads the link the node uses, from files only: an Ethernet interface up with carrier first (a
  `device`, `type` 1, not wireless), else the wireless one (`/sys/class/net/*/wireless`, `wlan0` first) with its
  `operstate`, the kernel's `carrier_down_count` (else `carrier_changes`) and, on Wi-Fi, the level from
  `/proc/net/wireless` (no program run for it).
- **The Wi-Fi power saving is kept off.** Every 60 s, and only while the link is Wi-Fi: `iw dev <if> get power_save`;
  when on, `iw dev <if> set power_save off`, read back. NetworkManager turns it on again at a reconnect — the next check
  turns it off again. Without `iw`: `iwconfig <if>` / `iwconfig <if> power off`. Neither: `unknown`, said once. Each
  program runs in a session of its own with a 5 s timeout; one that does not end is killed with its process group (a
  child it started cannot keep its output open), and one that will not die is left, no other started while it lives.
  It edits no file — nothing under /etc, no NetworkManager configuration — so a reboot or a reconnect puts the system's
  own setting back, and the next check turns it off again. A failure is logged once and never raised.
- **The heartbeat carries `network`** {interface, kind wifi | ethernet, connected, rssiDbm, signal (good ≥ −60 dBm, fair
  −70 … −61, weak < −70), powerSave off | on | unknown, powerSaveHeld, at} — read from memory (`with_network`); the
  heartbeat does no I/O for it. No interface (a Mac, a container without /sys/class/net): no block, as before.
- **Connectivity gaps.** `HttpReporter._open` is now the one call every request of the reporter makes (the ingress
  posts, getProgram, reportHardware, getBootState): an answer of any status — the request reached the cloud — gives its
  Date to the server-time reference, then tells the `LinkWatch`; a request with no answer (no connection, a timeout, a
  name that did not resolve) tells it so. At the first answer after the last one, when a request in between got none
  and the two are 60 s or more apart, ONE `connectivity_gap` (source node, no card) goes through the node-event queue —
  ordered, retried, with an eventId: `payload.connectivity` {from, to — the backend's time from the two answers' Date,
  seconds by the monotonic clock, interface, kind, wifiConnectedThroughout / wifiDropped (a 5 s sample found it not
  associated, or carrier_down_count rose), rssiDbmBefore, signalBefore, powerSave — the last readings before the first
  request got none}. Nothing before the first answer since the start, nothing for a silence in which nothing was asked.
  The log says it in words ("no answer from the cloud for 5 min 10 s (…); its Wi-Fi stayed connected (the router or
  the internet dropped) — reported as connectivity_gap").
- `main.py`: `wire_network` (after the node events; before MQTT connects, so the power saving goes off as early as it
  can), the heartbeat extra through `with_network`, the thread stopped at the clean stop. Offline (`--offline`): none.
- Tests: NEW `tests/test_net_care.py` (67): the power saving on → off → read back, already off, NetworkManager turning it
  on again (off at the next check, not before), `iwconfig` without `iw`, neither (unknown, said once), a turning off that
  fails (said once, tried again), an `iw` that cannot read it, a program that does not end (killed) and one that will not
  die (left; nothing started while it lives), `run_tool` on a real script that sleeps; Ethernet first (and no program
  run), Ethernet without carrier, a bridge that is not Ethernet, Wi-Fi not associated; every vector of
  `nodes/vectors/network.json` (the `/proc/net/wireless` samples in the kernel's format, the signal words, iw / iwconfig,
  every gap timeline); the Pump house's 5 min 10 s on the real sampling with its Wi-Fi up and with it dropping; the real
  HttpReporter against a local ingress that stops answering — one gap posted once through the node-event queue with an
  eventId, an error answer counted as an answer, every request of the reporter reaching the watch; nothing on the
  runtime's thread and an `iw` that hangs never delays a pass (passes every 0.1 s, a switch carried out within a pass);
  main() wires it; every block and gap validates against the contract (AJV). `tests/test_v1_parity.py`: the version is
  0.4.2, and a status may carry `network` (checked against the contract's keys, then taken out; 0.3.4 never sends it;
  none on a Mac) — the comparison with 0.3.4 is otherwise unchanged.

## 0.4.1 (2026-10-18)

**The review of v1.15.3 (contracts v1.15.4, SYNC.md v1.15.4) — no reload, rollback or port test lets go of a driven
line; a line changes its use in two bundles.** Still 0.4.1 (not tagged, never released): the backend now migrates a
node, and serves it a v3 bundle, only on 0.4.1 or later — 0.4.0 has none of what follows or of what is below.

- **A language-1 reload keeps a dropped output driven at off** (`blocks/runtime.py _release_output`): switched off and
  its line request KEPT (`output_released`, level off) — 0.3.4 let go of it, and a freed line falls to its pull: a
  reload that unlinked or deleted the Pump house's Light (it runs v1 today) would have energised its active-low relay.
  An output that moved to another line keeps its old line driven at off too. The one behaviour of a language-1 bundle
  that differs from 0.3.4 on purpose (programs/README.md §4.8; the founder's call): `tests/test_v1_parity.py` still
  passes against `v0.3.4` — its session drops no output — and names it.
- **A port test leaves a line as it found it** (`ports/port_test.py`, `LineIO.held_direction`): a line the agent holds
  as an output (a dropped output kept driven at off) is written off and kept after a pulse or a read; any other line is
  let go of, as before.
- **A line changes its use in two bundles** (no agent change — 0.4.1's refusal is the last line): a reload that turns a
  line its program drives into one it reads, or the reverse, is refused and the running program stays; one that first
  drops the line (kept driven at off), then one that uses it the other way, goes through. The backend never writes the
  one-step bundle (README §7.2 *A line keeps its role*).
- Tests: NEW `tests/test_v3_line_roles.py` (9) — on the BCM2835 line model of `test_v3_port_direction.py`: a v1 reload
  that drops the Light keeps line 17 driven HIGH (Light on and off before; both fail on the code before this change,
  "a v1 reload let go of line 17"), the same on v3, a rollback whose v1 program does not drive the Light, a port test
  (pulse, read) of a held line keeps it driven (both fail before), a free line is let go of; the vector
  `rewire-swap.json lineRoleSwap` — the swap of the relay and the wall switch between lines 17 and 27 refused in one
  bundle (line 17 still driven), taken in two (line 27 drives the relay, line 17 reads the switch, the Light switches).
  Changed for the new behaviour: `tests/test_blocks_reload.py` (a dropped or moved output is switched off and kept
  driven, never released).

**Every port entry of a v3 bundle is complete, and the node refuses one that is not (contracts v1.15.3,
programs/README.md §7.2 and §7.5a; SYNC.md v1.15.3).** A patch on 0.4.0 (released 2026-10-17, tag `v0.4.0`,
`7078607`). Not tagged; releasing it — a tag and `/config/agent` 0.4.1 — is the founder's, and comes after the backend
deploy (SYNC.md v1.15.3, the rollout order).

**What happened (2026-10-17, the Pump house).** At 12:42:32Z the migrator served the Pi a v3 bundle whose only entry,
the Light's DO1, stated no direction: `{kind gpio, chip pinctrl-bcm2835, line 17, activeLow true}` (the port table
states none for a `do` row; the backend's v3 compile copied the transport alone). 0.4.0 took an entry with no
`direction` as an INPUT (`v3/program.py is_output_port`): at the switch it wrote line 17 off and closed its output
request (`host.py _switch_now`, "let go of"), then its sampler requested the line as an input with no bias — the line
fell to GPIO 17's pull-down and the active-low relay ENERGISED (on a pump the motor would have started) — and every
`setPower` was refused "is an input on this node". The rollback at 12:49:30Z (v4) did not re-drive the line either:
the v1 runtime remembered `False` from before 12:42 and wrote nothing until the next on → off.

- **An incomplete entry is refused** (`blocks/v3/program.py entry_refusal`, in `build`): no `direction`, one that is
  neither `in` nor `out`, an output that states no `activeLow`, a PWM port said to be an input. The bundle is rejected
  — `programAck rejected` with the node's words naming the card and the port ("refused: device.power of card
  P9tZJr59FfqFy05d6qNy (line 17 of pinctrl-bcm2835) states no direction — …; the running program stays") — before
  anything stops: no line is requested, written or closed.
- **A switch of language that would change a line is refused** (`blocks/host.py switch_refusal`, checked in
  `compile` with the other checks, before the hold): to language 3, a line the v1 program drives that the v3 program
  would not drive (let go of) or would read; any line that would change direction; a line both drive at another
  active level (inverted). The rollback (to language 1) is refused for a direction change or an inversion; a line the
  v1 program does not drive (a card with no v1 program) is written off and **kept driven at off** — never let go of to
  its pull (`output_released`, level off).
- **A reload of language 3** (`blocks/v3/runtime.py _take_over`) refuses a line the running program drives that the
  new one would read, or one it reads that the new one would drive (0.4.0 wrote it off and freed it as the take-over
  committed); an output the new program drops is written off and its line kept driven (`_flush` no longer releases);
  the same line driven the same way (`same_output_line`) is carried with nothing written, also when the new entry only
  states what the old one left out.
- **An output port is never opened as an input** (`blocks/io.py HardwareIO.read_input`): a read of a port whose entry
  says `out` returns no value and is said once in the log — it would close the line's output request.
- **Back on v1, an output the v3 program did not hand over is driven by the first pass** (`blocks/runtime.py
  take_over_language`): its last remembered write is dropped, so the line is requested as an output again at its
  level. The language-1 runtime is otherwise 0.3.4's, unchanged: `tests/test_v1_parity.py` (v1-light, v1-switch,
  v3-gate) passes against the tag `v0.3.4` as before.
- Tests: NEW `tests/test_v3_port_direction.py` (13) — the EXACT prod shapes from the contracts vector
  (`programs/vectors/port-direction.json` `prod`) through the real `ProgramHost`, `HardwareIO` and `LineIO` with a
  BCM2835 line model (a closed output request falls back to an input; GPIO 17 pulled down; an active-low relay): the
  bundle served at 12:42:32Z is refused and line 17 untouched (no request, write or close) while v1 keeps driving it;
  a complete bundle switches with the Light's level unchanged, then on = LOW, off = HIGH on v3; the rollback keeps the
  level; an output not handed over is driven again by v1's first pass; every incomplete entry refused in words; an
  output port never read. Against 0.4.0 (`7078607`) 10 of the 13 fail — "DID NOT RAISE CompileError" for the 12:42
  bundle. Changed for the new behaviour: `test_v3_corners.py` (a driven line that would become an input is refused; a
  dropped output is kept driven, then its line may be read; a moved output whose old line would become an input is
  refused), `test_v3_language_switch.py` (a switch to v3 that would let go of a driven line is refused; back on v1 an
  undriven line is kept driven off), `test_v3_runtime.py` (a card that leaves the bundle: its line kept driven; the
  default farm's Broadcom-numbered GPIO 17 is not the pump house's line 17 — that switch is refused), and every
  hand-written v3 entry states its active level (and an AI entry its direction), as the contract now requires. Whole
  suite: 1012 passed.

## 0.4.0 (2026-10-17)

**Bundle v3 — layout rules, the instruction, the built-ins (ADR-029, contracts v1.15.0, programs/README.md §7).**
A minor version on top of **0.3.4** (released 2026-10-16, tag `v0.3.4` — merged in on 2026-10-16, see "Built on
0.3.4" below): a new block language, a new capability, new commands and new events. Not tagged; releasing it — a
tag and `/config/agent` — is the founder's.

**Coexistence — nothing changes for a node until it is migrated.** The agent now reports
`capabilities.blockLanguage = 3`. The cloud serves a v3 bundle only to a node that reports 3 AND whose v1 NAB is
migrated (`migratedAt`, the founder's yes); every other node keeps its language-1 NAB bundle. So the agent runs
both: `blocks/host.py` `ProgramHost` hands a language-1 bundle (`language` absent or 1) to the unchanged
`BlockRuntime` — the same as the released 0.3.4 in everything the agent does, checked against the tag itself
(`tests/test_v1_parity.py`, below) — and a language-3 bundle to the new `blocks/v3/` runtime. Language 2
(superseded before it was built) is rejected. The changes to the v1 module are additive (an event `source`, a
shared lock, `loaded`; and, for the switch of language below, an after-pass hook and four read-only helpers). The
migration switch itself is checked first (a v3 bundle that does not compile never stops the v1 program) and, since
contracts v1.15.1, is **bumpless** — see "The switch of language" below (it replaced "the v1 runtime stops as on a
clean stop and v3 loads cold, as after a restart", which would have switched the Pi's Light off).

**Built on 0.3.4 (merged 2026-10-16).** 0.4.0 was first built on 0.3.3; the released agent — what the production Pi
runs — is 0.3.4, so `main` (`c3fbbec`, tag `v0.3.4`) was merged into `wave/2026-10-wave1` (a merge commit, no
history rewritten) on top of contracts v1.15.0 as merged with v1.14.2 (SYNC.md v1.15.0 items 5 and 8). Everything
0.3.4 brought is kept as released: the signed-command gate, the backend's time (`server_time`), command acks and
node events over HTTPS through their ordered queues, the refusal acks and their budgets, the archive OTA with no
git (probation, the verify-timer, the wait while an output is on, updates only forward), join safety (every status
names the running version), `config.yaml` never half written. The conflicts: this file (0.3.4 stays as released,
0.4.0 above it); `__init__.py` and `setup.py` (0.4.0); `blocks/clock.py` (0.3.4's review and wave 1 had both made
`SystemClock` probe on its first call — one form kept, 0.3.4's: nothing trusted before a probe has said so);
`handlers/gpio_handler.py` (both: wave 1's input pull, 0.3.4's ON/OFF record for the OTA wait). `main.py`,
`blocks/runtime.py` and `blocks/io.py` merged on their own. What the two halves did not know of each other, found by
the merged suite and the comparison below, and joined here:

- **An OTA update could never have been installed on a 0.4.0 node.** 0.3.4's `main.outputs_on_for` asked the runtime
  for `nab`, and 0.4.0's runtime is a `ProgramHost`, which has none: every `firmwareUpdate` would have read "the
  outputs could not be read" as ON, waited its full time and failed busy — a node that can only be updated from its
  terminal (the founder's rule: never again). Now `ProgramHost.outputs_on()` answers for the program that runs: a
  language-1 program exactly as 0.3.4's `BlockRuntime` (a test drives both through one session and compares every
  second), a language-3 one (`RuntimeV3.outputs_on`, new) its motors and other outputs at an on level, every valve
  that is open — a continuous one whose `valve.open` is on, a latching one recorded open — or has a pulse in
  progress or a coil not known off, and every schedule run that is running (a restart switches all of these off, and
  closes an open latching valve at its boot pass). The probation heartbeat reads the same.
- **The wave-1 commands on a node that is not migrated.** `resetValve`, `pauseAutomation` and `resumeAutomation` went
  to the language-1 runtime, which answered `unknown command …` where 0.3.4 answers through the legacy executor (an
  `acknowledged`, then `No matching pin for command …`). They now reach the runtime only while a language-3 program
  runs; otherwise they take 0.3.4's path, so a 0.4.0 agent that is not migrated answers every command as 0.3.4 does
  (the comparison found it; it fails without the fix). Either way the gate has checked them first: every message on
  the command topic reaches `on_command` only through `mqtt_client._on_message`'s gate.
- **The built-ins' events in 0.3.4's event queue.** They already went through it (`CloudSink.event` →
  `publish_event` → `NodeEvents`, a `sensor_event` with an `eventId`, `timestampUncertain` while no clock is
  trusted, the valve or motor card as `sourceKey` — functions.yaml 1.11.1); a full queue now keeps them (source
  `node`) as long as the protection trips they replace on a migrated node — a program event leaves first. A
  language-1 program raises none, so nothing changes for it.

**Proved against the release itself** (`tests/test_v1_parity.py`, the session in `tests/parity_drive.py`): the whole
agent — `main()` — is run twice through the same scripted session, once as the released 0.3.4 (`git archive v0.3.4`)
and once as this tree, with fakes for the broker, the ingress (every endpoint), the gpiochip, the OTA install and
the clock signals, and everything each did is compared: every line opened, written or let go of (and read, in the
first session), every MQTT publish, every HTTPS request with its token, every OTA update with what it named as ON.
The session is the production node as it is today, rebuilt from the contracts (no prod read): one card, Light
(`aab_lighting_circuit` without the wall switch it has no port for), `device.power` on DO1 = line 17 of
pinctrl-bcm2835, active low, a port-table node; signed `setPower` / `setPortValue` on and off, `setVariable` (ui,
source, not settable), `runPlot`, `stopPlot`, the three wave-1 commands, `liveWatch`, `loraPairingWindow`,
`loraDownlink`, `readSensor`, an unknown type, a stranger's card, a bare GPIO number, four `testPort` (the Light's
line through the program, a free line pulsed and read, the Light named on a line it is not linked to), `scanBus`,
`syncPorts`, a `syncProgram` reload, two `firmwareUpdate` (the light on: it names `Light (device.power)`; off:
nothing), `syncPinConfig`; then the gate's refusals — unsigned, forged, replayed, stale, a stale `firmwareUpdate`,
another node's token, not JSON; a clean stop; a restart that loads the persisted program and switches the light on.
A second session runs the whole default block with its wall switch on DI1 (three toggles, a reload). Result: 285 +
55 records, identical in every stream; the only differences are the version each names and
`capabilities.blockLanguage` — 3 in every status of 0.4.0 that carries capabilities (the online ones), in none of
0.3.4's — and both are checked before they are set aside. The comparison was shown to catch a changed line level, a
changed ack text and a blockLanguage in a 0.3.4 status. A third session (0.4.0 only) runs the golden vectors' pump
house (bundle v3) through the same `main()`: an unsigned, a forged, a stale, another node's and a replayed
`resetValve` move no coil and change nothing (the forged, stale and other-token ones are answered `failed` with
their `reason` over HTTPS; unsigned and replayed are only logged), the signed one sends one close pulse and posts
`valve_reset` (source `node`, the valve as `sourceKey`, an `eventId`); `pauseAutomation` / `resumeAutomation`
likewise; every status names 0.4.0, the online ones `blockLanguage` 3; after a clean restart the first status
carries Valve 2 `closed`. The five drives (two sessions × two agents, and the v3 one) take about 50 s, run in
parallel; skipped where git or the tag is missing (a release archive on a board).

**The switch of language — the migration and its rollback (contracts v1.15.1, programs/README.md §7.5a; same
version, still not tagged).** The backend's migrator (agal-one `scripts/migrate-nodes-v3.mjs`) moves a node to bundle
v3 once, with the founder's yes, and can roll it back; the node's half:

- **Bumpless, both ways** (`blocks/host.py` `_switch`). The old runtime stops **without** its clean-stop safe state;
  the new one loads cold (the built-ins from the node's own persisted v3 state) and takes over every output the two
  programs share — the same card and port, on the same line driven the same way (`io.same_output_line`: one physical
  port, the same `activeLow`, the same kind) — at the level the old one left: nothing is written for it at the first
  pass. A continuous valve's `valve.open` comes from its v1 coil (open = the coil at its open level for
  `normallyClosed`), and goes back to it; a latching valve is never carried. A motor carried on starts its guard's
  inrush window at the switch. It is not a boot (no `boot` event, no restart resume, no runs carried); rules,
  variables and sequences start afresh; a schedule id that ran or was missed today keeps that (`lastRunDate` /
  `missedDate` by id), so a switch at 06:40 does not run the 06:00 schedule again. A line the new program does not
  drive is switched off and let go of (`output_released`), as at any reload.
- **Between runs only.** While the running program has a run in progress — a v1 schedule or plot run, an end still
  due, an output a reload let go of, a command not applied yet; on v3 also a run interrupted by a power cut, an
  output kept to the end of its run, automation paused, a wait for the power — the host holds the checked bundle
  (`pending_version`) and switches at the end of the first pass with none. While a runtime thread runs passes the
  switch is always made from that thread, after its pass (the runtimes' new `after_pass` hook), so no pass can
  start a run between the check and the switch. A bundle of the running language that arrives meanwhile (a rollback
  before the switch) replaces the held one. The ack (`program_applied`) comes at the switch.
- **A v3 program reports its cards' outputs** (`RuntimeV3._report_outputs`, functions.yaml 1.11.2
  `variables.outputs`): after every load and whenever a level changes, `{sourceKey: level}` per card — a valve by
  `valve.open` — through `CloudSink.outputs` and the same budgeted HTTPS queue as a v1 card snapshot (no MQTT copy).
  Found while checking what the Pi relies on: a v3 program reported nothing, so the card's `state.variables.relay` —
  which the app's live layout reads before `state.power` — would have frozen at its last v1 value. A v1 snapshot is
  posted exactly as before (no `outputs` key).
- **A port test of a plain output goes through the v3 program**, as on v1 (`ProgramHost.port_for_line` →
  `RuntimeV3.plain_output_on_line`): found on the Pi's Light, whose DO1 test v3 refused as busy. A motor's line (its
  guard supervises it) and a valve's coils (its driver's) stay busy.
- A line the v1 program left on that the v3 program uses as a latching valve's coil is not known off: written off
  with the first pass's switch-offs, before any pulse ("never both coils at once", as in a v3 swap).
- Tests: `tests/test_v3_language_switch.py` (13 then; 16 with the review below) — the Pi's Light on across the migration and back with nothing
  written, its off state reported, its port test; a pump running and a valve open carried across and back; the switch
  held through a v1 run and the schedule not run twice; a held switch replaced by a rollback; a line let go of; a
  latching coil written off before any pulse; a bad bundle keeping v1; a runtime thread switching after its pass; the
  outputs on the wire. Each was shown to fail without its part (the carry in either runtime, the wait, the
  no-safe-state stop, the coil).
- **Review of the switch (contracts v1.15.2, SYNC.md v1.15.2 item 1; same version, still not tagged).** Three paths
  where the switch broke its own promises, each found by running the real agent and fixed:
  - *Two runtimes at once.* The switch runs on the old runtime's thread; between the old runtime's stop and the new
    one's start the host looked stopped, and `main.py`'s once-a-second "a program that arrived after boot starts the
    runtime" check started the OLD runtime again — whose `stop()` then joined the new thread and timed out. Both
    languages then ran passes, in both directions, until the process exited (14 of 40 trials at real speed; on a
    synthetic farm both wrote the valve and the pump lines). Now `ProgramHost._switching` is set under the lock from
    the first step of a switch to the new runtime's start: `_running` is True meanwhile and `start()` (under the lock)
    starts nothing; `BlockRuntime.stop` / `RuntimeV3.stop` read the threads to join with the flag, under the lock, so
    they never join a thread a later start made.
  - *A held bundle stored as the node's program.* `ProgramSync` saved a bundle of the other language as `program.json`
    while the host only held it: a restart during the hold booted that language cold, mid-run — the migration
    watered the run again from scratch, the rollback ran schedules the owner had paused. Now only the running bundle
    is stored: a held one is not, and `ProgramHost.on_switched` stores what runs when the switch is made (read under
    the store's new lock, so a newer pull is never overwritten); the next pull after a restart hands the held bundle
    again.
  - *The E-stop latch at the switch itself.* `compile` refused to leave language 3 while latched, but the switch is
    made at the end of a pass and asked only `busy_reason()`: an E-stop pressed in the pass that ended the hold let
    the node switch to v1 and drop the latch, and a remote switch-on ran the pump with no panel reset. Now the hold
    waits while it is latched ("the E-stop is latched at the panel") and `_switch` checks it once more, under the
    lock, before the v3 runtime stops.
  - Tests (3 more in `tests/test_v3_language_switch.py`, each failing on the code before this change): main's check
    every millisecond against four slowed switches leaves exactly one runtime; a restart during a held migration
    boots v1, keeps `program.json` at v1 until the switch after the run, and v3 does not start the 06:00 run again;
    an E-stop latched in the pass that ends the hold keeps the node on v3 until the panel reset. The review's own
    harness (scratch `compat-w1`): `exp_switch_race.py` 0 of 40 trials with two runtimes (was 14), the held
    restarts now boot the running language, `exp_estop_switch.py` keeps v3 and refuses the pump.
- **A pass never waits on the network; never two runtime threads (the review after v1.15.2, 2026-10-17; contracts
  v1.15.2 as amended — programs/README.md §4.8 and §7.5a, SYNC.md v1.15.2 item 1 (d)–(f); same version, still not
  tagged).** The end-to-end confirm run (the real `main()` against the functions emulator) found three more:
  - *The network inside a pass.* The first pass after any program load or switch of language posted its
    `programAck` over HTTPS (`HttpReporter.report_program_ack` → `urlopen`, 10 s timeout) on the runtime thread,
    holding the runtime lock, and so did every pass that raised an alert (`report_alert`) — and the host's
    "could not take over" ack. Released 0.3.4 does the same for a v1 program. Meanwhile the input sampler, every
    command and, on v3, the E-stop latch and the motor guards waited. Now `CloudSink.program_ack` / `alert` hand
    both legs (HTTPS, and the MQTT copy) to `blocks/cloud.py` `_Outbox`: one worker thread, in the order they were
    handed over, each tried once and logged as 0.3.4 did (no retries added; `last_ack` — the heartbeat's
    `programStatus` — is set at once). ProgramSync's `rejected` goes through the same outbox (`ack=`, HTTPS only, as
    before), so a later `rejected` never overtakes an `applied` still waiting. Bounded: past 256 waiting, the oldest
    alert leaves first. `CloudSink.flush` covers it; `http_async=False` (tests, the simulator) posts inline as before.
    With the ingress stalled (accepting, never answering; scratch `compat-w1/exp_stalled_safety.py`), 37577d1 → now:
    a `setPower` answered after 10.003 s → 0.000 s (v1 and v3); an E-stop pressed during an alert's pass stopped the
    pump after 10.004 s → 0.003 s; an over-current with a 1 s hold after 11.013 s → 1.007 s.
  - *Two runtime threads at once.* The switch ran on the old runtime's thread: it started the new runtime and then
    waited for the lock while the new one's first pass posted its ack (25 ms – 4.7 s of overlap in all 6 runs). Now
    the old runtime's thread only decides — under the lock, at the end of a pass with no run in progress — and runs
    no pass after it (its flag is down); a `language-switch` thread joins the old runtime's pass loop and sampler
    (bounded: `SWITCH_JOIN_S`, 10 s; past it the switch says so and goes on — a thread left over can begin no pass)
    and only then takes over and starts the new runtime, in one hold of the lock with the let-go and the store, so
    the host never reads as stopped. With no runtime thread running (a pull while stopped, the tests' `step()`) the
    switch is made on the caller's thread. A command, a compile or a stop that arrives during the switch waits for it
    (the lock let go of meanwhile, `SWITCH_WAIT_S`) and goes to the program that runs after it: before, a command
    the old program accepted in that moment was queued on a runtime that ran no pass again — lost; a stop now stops
    the runtime that took over (clean, with its state recorded). `exp_switch_race.py` (scratch, now with a 0.5 ms
    probe and a check at every runtime start): 20 of 20 trials with both threads alive before (5 of 10 with a
    stalled ingress, for seconds), 0 of 20 after — 0 of 20 stalled too.
  - *The v1 program's cards after a rollback.* `BlockRuntime._var_last_reported` survived the v3 period, so the
    first pass after a rollback skipped a card whose snapshot equalled the one sent before the migration — and the
    backend had removed `state.variables` while v3 ran: absent until a value changed. Now a take-over of language and
    every runtime start forget what was reported (`_forget_reported`: every card dirty, reported in full at the next
    pass); the v3 runtime likewise reports every card's outputs after every start. A v1 node that never switches
    reports exactly as before (the comparison with 0.3.4 above passes unchanged).
  - Tests: `tests/test_pass_never_waits_on_network.py` (10) — the outbox's order and its bound; ProgramSync's
    rejection behind a pass's ack; with the stalled ingress and the real HttpReporter, AgalOneMqttClient, NodeEvents
    and CloudSink: a v1 `setPower` within one pass (1 s), a v3 `setPower`, an E-stop during an alert's pass within one
    pass, an over-current within its 1 s hold + one pass; no runtime thread alive at any runtime's start across four
    switches (and a 0.5 ms probe never seeing both), on the Pi's I/O path with its samplers; a command during a switch
    carried out by the new program; a stop during a switch; the full report after a rollback and after a start. All
    10 fail on 37577d1. The v1 comparison (this run): 289 + 55 records identical in every stream, no adjustment (each stream
    keeps its order; streams were already compared one by one).
- **Nor on the disk, nor on a clock probe; a report keeps its pass's time (the second review, 2026-10-17; contracts
  v1.15.2 as amended — programs/README.md §4.8, SYNC.md v1.15.2 item 13; same version, still not tagged).** A latency
  review with a simulated slow SD card and a slow clock probe (scratch `review-latency/exp_slow_sd.py`: the real
  ProgramHost and RuntimeV3 on their threads, a 1 s pass, no network at all) found:
  - *An fsync under the runtime lock (v3).* `RuntimeV3._persist` wrote the state file and fsynced it on the caller's
    thread, and every caller holds the runtime lock: the end of every pass that changed what the node must remember (a
    `setPower`, a run's start or end, the E-stop pressed or cleared, a guard trip, learn or expiry, an output let go
    of, pause / resume), every 60 s during a run, and the switch's clean stop. Now the record is built under the lock
    and handed to `_StateWriter` — one per state file for the whole process, one thread that writes, fsyncs and
    renames, the newest record replacing one not yet picked up, records landing in the order they were made. The end
    of a pass on the runtime's own thread does not wait; every act whose next physical step depends on its record
    still waits for it through the same writer (a latching pulse's mark and position, a valve reset, the cold start's
    not-clean record, a clean stop), and so does `step()` from the tests and the simulator; reading the file
    (`_read_state`) waits for every record handed over before it. The switch v3 → v1 hands its clean-stop record over
    without waiting (it is read only at a later v3 load). A write that fails is logged once by the writer and said to
    the next caller that waits (no latching pulse then, as before). v1's `_persist` (no fsync) is unchanged.
    `exp_slow_sd.py` with a 1 s fsync, 36c366a → now: an E-stop pressed 20 ms after a `setPower`'s pass stopped the
    pump after 0.972 s → 0.002 s; an over-current with a 1 s hold 1.974 s → 1.003 s; a command 20 ms after the
    E-stop's pass answered after 0.989 s → 0.000 s; no runtime pass possible at a switch v3 → v1 1.007 s → 0.001 s.
    **Left:** v1 → v3 still has one fsync with no runtime (1.007 s with a 1 s fsync): the cold start's not-clean
    record must be on disk before the switch stores the v3 bundle — handed over without waiting, a crash before it
    landed would boot v3 on an older record that vouches for positions the v1 program has changed since; written
    earlier, when the held bundle is checked, every migration would load cold after an "unclean stop" (every latching
    valve in FAULT). A migration is once per node (the founder's yes), a rollback rarer.
  - *`timedatectl` under the runtime lock.* `SystemClock.clock_ok()` probed on the caller's thread once a minute
    (`timedatectl` over D-Bus, timeout 3 s, on any board with no `/dev/rtc0` — a Pi 3B with no DS3231), and both
    runtimes ask on every pass under the lock (v1 since 0.3.4; v3's `_today` / `_midnight`), as does NodeEvents with
    no backend time yet. Now only the first probe runs on the caller's thread — `main.py` makes that call for both
    of its clocks at start-up, before the persisted program loads and before MQTT connects, so the first answer is
    still a probe's — and every later one on a `clock-probe` thread while the callers get the last answer (one probe
    at a time). With a 1 s probe: an E-stop pressed 20 ms into it stopped the pump after 0.974 s → 0.002 s; a
    command answered after 0.980 s → 0.000 s.
  - *Reports stamped when posted.* `HttpReporter.report_program_ack` / `report_alert` (and the MQTT copies) stamped
    `timestamp` inside the post, which since the outbox runs behind the posts ahead of it (10 s each against a stalled
    ingress) — and the backend files the `automation_alert` event and `program.appliedAt` at that time. Now
    `CloudSink.alert`, `program_ack` and `post_program_ack` stamp the hand-over and both legs carry it (an optional
    `timestamp=` on the four calls; other callers unchanged). `exp_outbox_stamps.py` against an ingress answering
    after 2 s: the second and third reports +2.018 s / +4.029 s → +0.000 s.
  - *Parity coverage.* Neither parity session raised an alert, so the alert path moved into the outbox had no 0.3.4
    comparison. The v1-light session's NAB now reads the Light's relay and raises "The light is on" (its default
    300 s cooldown): 293 + 55 records identical in every stream, `https telemetryIngress alert` 2 and
    `mqtt …/status alert` 2 among them, and the session test asserts both.
  - Tests: `tests/test_pass_never_waits_on_disk.py` (7, all failing on 36c366a): with a 1 s fsync the E-stop, the
    guard and a command keep their time and every fsync is on the writer's thread; records in order, the newest wins,
    a read waits; a failed write is said to the waiting caller and logged once; the v3 → v1 switch does not wait for
    its clean-stop record and a later v3 load reads it; only the first clock probe on the caller's thread; an E-stop
    during a 1 s probe; a report stamped at its hand-over. Whole suite: 977.
- **A valve reset is answered by the pass that carries it out (contracts v1.15.2 item 15 — functions.yaml resetValve
  1.11.3, programs/README.md §7.9 "The reset's answer"; same version, still not tagged).** `main.py` acked a
  `resetValve` `completed` the moment the v3 runtime ACCEPTED it — before the close pulse of the next pass — and a
  reset that pass refused (the supply dropped meanwhile, the open coil could not be switched off, the pulse could not
  be recorded) was only logged; a close pulse that could not be sent raised into the pass's log. So the
  /assetCommands document said completed while the valve stayed in FAULT (SYNC.md v1.15.2 12(e)). Now:
  - *Nothing on arrival.* The `on_command` branch for the program's own commands is a function,
    `main.answer_program_command`: a refusal on arrival is acked `failed` in the runtime's words and every other
    accepted command `completed`, as before — but an accepted `resetValve` is left to its pass (no ack: the document
    stays `sent` for that pass, ≤ 1 s, so its one post is the final one).
  - *One final answer, from the pass.* `RuntimeV3._cmd_reset` returns its outcome; the pass records it and hands it
    to the sink after its events (`step()` is now `_step()` and then `_send_acks()`, in a `finally`, outside the
    runtime lock — an answer is handed over even when the pass fails later on). `completed` once the close pulse has
    run, the valve is recorded closed and its FAULT is cleared (after that pass's `valve_reset`); `failed` in the
    node's words when the pass refuses it (the supply not sensed present; not a latching valve of this node — a
    reload) or cannot carry it out: "the open coil could not be switched off: no close pulse sent — <valve> stays in
    fault"; "the reset could not be recorded in the state directory: no close pulse sent — …"; "the close pulse could
    not be sent — …"; the supply not present after the close pulse (new, as after any pulse in `_pulse`: the valve
    stays in FAULT, `pulse_cut_short` — before, the reset's pulse alone was taken without that check); "the reset
    could not be carried out: <error>". A reset accepted but never run — the runtime stopped first — is answered
    `failed` at `stop()` ("the node stopped before the reset ran: no close pulse sent", `RESET_STOPPED`) and dropped,
    so a later start never pulses it (and, since the review below, one that arrives after the stop is refused on
    arrival).
  - *Idempotent.* A reset of a latching valve that is not in FAULT is no longer refused ("refused: Valve 2 is not in
    fault"): it is accepted and its pass answers `completed` with no pulse — two people pressing Reset together, or a
    press while the card still shows the fault (the callable refuses `not_in_fault` from the cloud's mirror). The same
    message twice is still refused `replayed` by the gate. A latched E-stop is still no refusal (§7.10).
  - *Through the ack queue every ack takes.* `EventSink.command_ack` (new: a no-op by default, recorded by
    `RecordingSink`); `CloudSink.command_ack` → `AgalOneMqttClient.publish_command_ack` → `command_acks.CommandAcks`
    (its worker posts it, retried 5 minutes) and the paho copy (queued). A pass never waits on the network: with every
    HTTPS post stalled, the reset's pass pulsed within the same wake, the E-stop latched at once, and the `completed`
    was posted when the ingress answered.
  - *The port, not the card.* The supply refusal said "(sensor.power of card asset_ups00001)"; it now names the port
    as the node knows it — "refused: the valve's supply on line 13 of pinctrl-bcm2835 is not present"
    (`io.describe_port`, as the one-port-one-card refusal). The bundle carries no node port id, so the node cannot say
    "DI3" (SYNC.md v1.15.2 item 15(c)); a commandAck has no structured field for the card, so no card id is kept.
    Not changed: the `valve_fault` detail ("the supply on <sourceKey> of card <assetId> dropped").
  - Tests: `tests/test_v3_reset_ack.py` (15) — completed only by the pass that sends the close pulse, after its
    `valve_reset`, nothing on arrival; completed through a latched E-stop; refused at the next pass (the supply
    dropped after arrival) in the node's words, no pulse; the words name the port and no card id; refused at the pass
    after a reload made the valve continuous; two resets in one pass and a third later: one close pulse, three
    completed; failed for a close pulse not sent, the supply gone after the pulse, the open coil stuck on, a state
    directory that cannot be written, an error in carrying it out; answered even when the pass fails after it;
    failed at a stop with no pass, and never pulsed after; `main.answer_program_command` leaves an accepted reset to
    its pass and answers every other command at once; with the real `AgalOneMqttClient`, `CommandAcks`,
    `NodeEvents` and `CloudSink` and every post stalled, the runtime on its own thread at the 1 s tick pulses at once,
    latches the E-stop at once, and posts the `completed` when the ingress answers. `tests/test_v3_valves.py`: the
    not-in-fault reset is completed with no pulse, and the supply refusal's words. Whole suite: 992 (after 07:00
    IST — see SYNC.md v1.15.2 item 15(e)). Parity: v1-light 293 records identical in 22 streams, v1-switch 55 in 16;
    the v3-gate session's signed reset: close pulse, then `valve_reset`, then one `completed` commandAck.
- **The review of the reset's answer (contracts v1.15.2 item 16 — functions.yaml resetValve 1.11.3, programs/README.md
  §4.4 and §7.9; same version, still not tagged).**
  - *One reset per valve per pass.* Two resets of the same latching valve in one pass (two people pressing Reset, or
    the app and Agal Assist), the supply read absent right after the first one's close pulse: the first was failed
    `pulse_cut_short`, but the second was checked by `_refuse_reset` against the supply as SAMPLED at the start of the
    pass — still present — and sent a second close pulse with no supply behind it (the review's scratch
    `exp_reset_edges.py` X1: 2 close pulses, both failed). `RuntimeV3._apply_commands` now keeps each valve's answer
    for the pass: a second reset of the same valve takes the first one's answer and sends no pulse of its own — also
    after a close pulse that could not be sent, or a carrying out that raised (no retry in the same pass). The next
    pass samples the supply afresh and a reset then is refused on arrival in the supply's words.
  - *No command accepted once the runtime has stopped.* `main()`'s `finally` stops the runtime first, then flushes the
    sink and closes the ack and event queues (3 s each), and disconnects MQTT last: for those seconds the paho thread
    still handed commands to the v3 runtime, which accepted them (`stop()` left the program loaded). A reset was never
    answered — the document `sent` until the backend's `checkCommandTimeouts`, where the contract promised the node's
    words — and stayed pending for a later start of that runtime to pulse (X3); a `setPower` was acked `completed` by
    `ports/links.handle_switch` and never applied. Now `stop()` marks the runtime stopped (`_stopped`, with
    `_running`) and only `start()` clears it — a compile after the stop (a program pulled during the shutdown) runs no
    pass either — and `apply_command` refuses every command on a stopped runtime in words: "refused: the node is
    stopping — no close pulse sent" (`RESET_STOPPING`) for a reset, "refused: the node is stopping — no pass will
    carry it out" (`STOPPING`) for every other; `answer_program_command` and `handle_switch` ack them `failed` while
    the ack queue is still open. A switch of language starts the runtime it hands over to, so a migrated or rolled
    back node is unaffected (scratch check: v1 → v3 → v1 → v3 on the runtime threads, then a `setPower` and a
    `pauseAutomation` accepted; after `ProgramHost.stop()` both kinds refused). The language-1 runtime is 0.3.4's and
    unchanged.
  - Not changed (as the review found): a restart in the middle of a reset's close pulse leaves its document `sent`
    until the backend's timeout (the restarted node puts the valve in FAULT `pulse_cut_short` and raises
    `valve_fault`); commands other than a reset accepted before a stop and run by no pass are kept, as before.
  - Tests: `tests/test_v3_reset_ack.py` (20, +5) — two resets in one pass with the supply gone after the first close
    pulse: one close pulse, two failed answers in the same words, and the next reset refused on arrival; a second reset
    does not retry a close pulse that could not be sent, nor a carrying out that raised; after `stop()` a reset, a
    `setPower` and a `pauseAutomation` are refused on arrival (also through `main.answer_program_command`), nothing is
    left pending, a compile does not end it, a start does (the reset then completed by its pass); through the
    `ProgramHost` main() holds, a reset after `host.stop()` is acked failed. All five fail on 37bd989. Whole suite: 997.
    Parity: v1-light 293 records identical in 22 streams, v1-switch 55 in 16 (4 passed).

**The pass (§7.5):** inputs → the built-ins (power sense, E-stop latch, valve drivers, motor guards), then the
commands and which blocks hold → each layout's active instruction part → the rule slots → outputs written once,
through the built-ins' veto, switch-offs first (motors before valves), then switch-ons (valves before motors), each
class in `assets[]` order. Every card the bundle carries is sampled; digital inputs also every 100 ms between passes.

- **The motor guard** (`v3/guard.py`, §7.11), always on, evaluated before every block. Dry run (the current
  `dropPct` below the learned baseline for `holdSeconds`), overload (above `multiple` × the rated current, or ×
  the learned running current when no rated current is set — 1.25 × by default), phase loss (three phase, where
  phase sense is wired) and imbalance (a current per phase), the voltage window where voltage is measured. Current
  is not judged during `inrushIgnoreSeconds` after each start. The baseline is learned once — the first 10 s after
  the inrush of a run that then runs a minute with no trip — kept on the node, never from a run that tripped. A
  trip: the motor off in that pass (the veto), the run of the block that had it on ended (`watering_not_finished`,
  `guard_tripped`), the restart lockout (never under 30 s, whatever a bundle says) during which nothing starts it — not
  a rule that keeps asking, not a command. A motor card without a guard, or a block that drives a motor port other
  than the guarded one, is refused. Ported from `sensors/protection.py` (the confirm window, the post-inrush
  average) without its thread or its relay writes.
- **The valve drivers** (`v3/valve.py`, §7.9). Blocks set `valve.open`; the coils are the driver's alone (a block
  naming one is refused). Continuous: one output, normally closed = powered open, inverted for normally open,
  never faults. Single-pulse latching: an open and a close output, one pulse of `pulseMs` (default 100), never both;
  the position and a pulse-in-progress mark are written to the state directory before and after every pulse. FAULT
  on a supply that drops or cannot be read (`supply_lost`), a start after an unclean stop (`unclean_boot`), a pulse
  cut short (`pulse_cut_short`), a first start or a changed configuration (`position_unknown`). While in FAULT no
  pulse is sent and only the blocks that use the valve hold (write, stop or read its `valve.open` — reading its
  built-ins is not a use), their outputs off, motors first. `resetValve` is refused while the supply is not sensed
  (and, until "A valve reset is answered by the pass" above, when the valve is not in fault — now answered completed
  with no pulse); otherwise one close pulse, recorded closed, and the held blocks resume by
  their schedules' own clock (`watering_resumed` cause `valve_reset`, or `run_cut_short` + `watering_not_finished`
  `valve_fault` once their planned end has passed). No time is added.
- **The E-stop** (`v3/safety.py`, §7.10). The aux contact (`nc` or `no`; an unreadable input reads as pressed)
  latches: every motor off, the blocks that drive a motor hold, `estop_pressed`. No command, role or rule clears it:
  a motor start is refused `refused: the E-stop is latched at the panel`. It clears only at the panel — the input
  reads released, then a rising edge of the reset input or the panel start's contact (a reset already held down
  does not count). The latch survives a restart. Held blocks resume by their own clock (`watering_resumed` cause
  `estop_cleared`; a run whose planned end passed is logged `run_cut_short` + `watering_not_finished` `estop`).
  Valves may still close and be reset while it is latched.
- **Power sense** (§7.8). `power_cut` / `power_restored` come from the mains (and UPS) sense inputs only — the clock
  coming good no longer raises `power_restored`. A mains cut interrupts every running run and switches motors off,
  then the continuous valves; a latching valve stays where it is. 30 s after the power is back (fixed) each run
  resumes for the time it had left inside its window (planned start → planned end + its `startWindowMinutes`),
  its start actions applied again; a run whose window passed, or that the window cuts, is logged `run_cut_short` —
  a notice, never `watering_not_finished`. A restart is the same with cause `restart`, 30 s after the boot pass
  (the runs in progress are kept in the state directory, refreshed every minute while one runs).
- **The bumpless swap and the instruction switch** (§7.5–7.6), building on 0.3.3's take-over: atomic under the
  runtime lock, rejected with every piece of state put back when it fails half-way; outputs carried by (card,
  port) — a valve by its `valve.open` — and nothing written for one that keeps its level; variables by (block
  key, name), rules by (block key, rule id) when unchanged; the built-ins' state never reset; running schedules
  carried by (layout, schedule id) across the layout's parts — start kept, end = start + new duration, only the new
  start actions applied at the switch pass — or, when gone, their old end actions at their planned end. An output
  no block writes any more keeps its level to the end of the run that set it, or `now` (the handover's `now`, or no
  run in progress); an output that is off is let go of (`output_released`). The active part is the today-only part
  of the node's local date, else the every-day one; at the node's local midnight the today-only part ends and runs
  are carried or ended by the same rule (`instruction_switched`, cause `midnight`).
- **Pause-first** (§4a, §7.7) — built here for v3: `pauseAutomation {until?, stopAll?, then?, actor}` /
  `resumeAutomation {actor}`. While running, a command on an output a block writes is refused `automation_running`;
  an output no block writes is switched directly (with `minutes`, timed on the node). While paused the instruction
  parts and the ordinary rule slots wait, the built-ins and the interlock slots run; resume runs the waiting end
  actions, re-applies the running schedules' start actions, switches off what was left on by hand (pump first) and
  restarts the ordinary rules as after a program load. A timed pause resumes on the node's clock.
- **Commands on a v3 node:** `resetValve`, `pauseAutomation`, `resumeAutomation`; `setPower` / `setPortValue` name a
  valve by `valve.open`; `runPlot`, `stopPlot` and `setVariable` are refused. A port test on a line the v3 program
  uses is refused as busy (a guard or a valve driver owns it). The node's own check (RBAC-01) sees every card port
  plus each valve's logical `valve.open`.
- **Heartbeat** (functions.yaml 1.11.0): `capabilities.blockLanguage` 3, and while a v3 bundle runs `safety`
  {estop, power}, `valves`, `guards`, `held`, `automation` and the active instruction part per layout. The built-ins'
  events (`valve_fault`, `valve_reset`, `estop_pressed`, `estop_cleared`, `guard_tripped`, `power_cut`,
  `power_restored`, `watering_resumed`, `watering_not_finished`, `instruction_switched`) go out with source `node`;
  `CloudSink.event` gained the `source` argument. A command's `actor` becomes the events' Author with the node's own
  `at`.

Where the contract left room, the agent does this (each is in the code, in words, where it happens):
a guard reports itself tripped for as long as its lockout runs (nothing else would ever clear it); a baseline under
0.1 A is not learnt (a motor that draws nothing is not running — the next run learns); motors stay off from a mains
cut until the 30 s resume (and for the 30 s after a restart with runs to resume); the ordinary rule slots restart as
after a program load when runs resume after a cut (found while testing: with a latching valve open through the cut,
"a valve is open" never went false and the pump never restarted — its test fails without the fix); a run resumed
while its part holds (a latching valve in FAULT after an unclean boot) is announced when the hold clears, not
before; after a restart every output starts off (a latching valve known open gets one close pulse); a valve reset
when it is not in fault is refused (since "A valve reset is answered by the pass" above: completed, no pulse).

**Review fixes (same version, still not tagged).** A review of 0.4.0 found paths where a built-in could be lost or a
line left driven; each is fixed, with a test in `tests/test_v3_corners.py` that fails without its fix:

- **The E-stop latch is never taken off on the node** (§7.10). A program without the E-stop is refused while it is
  latched — a take-over, a cold load after a restart with the latch persisted, and a switch back to language 1 alike
  (`refused: the E-stop is latched at the panel …`). Before, the latch belonged to no program after such a
  recompile: the held blocks resumed and the pump restarted with no reset at the panel. An unreadable state file is
  read as latched (the safe reading, as for an unreadable contact).
- **Debounced inputs at boot**: the first reading of a digital input is its value (a debounce filters changes). It
  was withheld, so with `debounceMs` on the E-stop or a supply sense every boot latched the E-stop and put each
  latching valve in FAULT.
- **A state directory that cannot be written** (a read-only SD card): no latching pulse is sent when its
  pulse-in-progress mark cannot be recorded (FAULT `position_unknown`), and a start that cannot record itself as not
  clean puts every latching valve in FAULT. Before, a restart could vouch for a valve that had opened since.
- **Never both coils at once**: each latching coil's line is tracked; the other coil is written off before a pulse
  unless it is known off, and no pulse is sent when it cannot be; a pulse that fails is followed by an off-write, and
  a coil not known off is written off with every pass's switch-offs. A pulse mark found at boot leaves that coil
  unknown (written off at the boot pass).
- **A line an old output drove** (§7.5): written off before the new program reads it as an input (at the commit of
  the take-over, before the sampler can read it), so `output_released {level: off}` is true; a line that becomes a
  latching coil — a continuous valve reconfigured as latching on its line while open — is written off before any
  pulse. Before, the line stayed driven.
- **A run interrupted by a power cut whose schedule a reload removes** runs its old end actions when the power is
  back, before the rules restart (`run_cut_short`, a notice). Before, a latching valve it had open stayed open and
  the pump restarted with no run to end it.
- **The guard**: its settings are held to the `MotorGuardSettings` bounds (a NaN or an out-of-range value is the
  default or the bound, logged), so a bundle cannot switch it off in all but name; a lockout restored after a
  restart is never longer than the lockout (on a clock that cannot be trusted it runs its full length from the
  boot); a card that becomes a guarded motor while its output is on is judged from the switch.
- **A clock that cannot be trusted** (no RTC, no NTP yet): interrupted runs wait to resume until it is trusted, or
  until their window has certainly passed by the monotonic clock (then `run_cut_short`) — the node is not held
  meanwhile; the node's date is the last date a trusted clock gave, and no part switches on an untrusted date; a
  timed pause is never more than 7 days ahead, and one restored on an untrusted clock waits for a trusted one.
  `SystemClock` probes on its first call (it answered "trusted" for the first minute of uptime without probing) —
  0.3.4's review found the same; the merge keeps 0.3.4's form.
- **A today-only part that keeps a schedule id at a later start** runs it, after the every-day run of that id;
  a run carried into a part that moves its start does not run twice.
- **An analog input is refused** as the mains sense, the UPS sense or a latching valve's supply: the node senses
  presence only on a digital input (the contract allows AI there, with no threshold — reported).

**Integration fixes (same version, still not tagged; contracts v1.15.0 as amended — `CompiledGpioInput`,
programs/README.md §7.2).** The wave-1 integration check (the backend's compiler fed into this runtime) found that
two of a port's wiring facts never reached the hardware. Both are fixed here, against a fake gpiochip in
`tests/test_input_pull_debounce.py` (29 tests); each fix, undone on its own, fails at least one of them.

- **`pull` is the line's bias** — a gap since 0.3.0 (1ef380a): `LineIO.read` took a `pull` and ignored it, and
  `HardwareIO` never passed one, so an E-stop input with no pull-up on the board floated. Now a GPIO input is
  requested with its entry's `pull` as python-periphery's `bias` — `up` → `pull_up`, `down` → `pull_down`, `none` →
  `disable` (stated wiring: an external resistor, so the internal pull is switched off — founder 2026-10-16); an entry with no `pull` is
  requested with no bias flag. That is the bias flag of the character device's line request (kernel 5.5+). A line is
  requested again when a reload changes its pull, and kept when a read asks for none. The legacy Broadcom-numbered
  path sets RPi.GPIO's `pull_up_down` the same way, and a port test that watches an input applies the port's pull.
- **README §7.2's default**: a GPIO input of an `emergency_stop` or `power_sense` card whose entry states no pull is
  requested pulled up (the reference compiler's rule, for a bundle from a compiler that left it out); a stated pull —
  `down` on a driven, active-high sense module, `none` — always wins.
- **A bias that cannot be applied** (a kernel older than 5.5, a library with no bias, a chip that refuses it): the
  line is requested once more with no bias, and a warning names the line and then the card's port. A **safety input**
  — an input of an E-stop or power-sense card, the E-stop's contact or reset, the mains or UPS sense, a latching
  valve's supply — is then never read from the floating line: it reads unknown. So the E-stop reads pressed and
  latches, its reset is never seen, and a sense is never sensed present: the power status stays `unknown`, and a
  latching valve on that supply goes into FAULT `supply_lost` and sends no pulse. This holds even when the contact is
  in fact closed. Any other input is read with no bias, with the warning.
- **`debounceMs` from the bundle's port entry, for every input.** The v3 runtime already read it there; the contract
  had no place for it, so it never arrived. With v1.15.0 it does: the field box's flow switch keeps its 200 ms on a
  v3 node (a 100 ms bounce on the line never becomes a value; a change that holds is taken after 200 ms). An input on
  a bus that states `debounceMs` (an I/O expander's, a Modbus discrete input) is now a digital input — read as on/off
  every 100 ms and debounced — not an analog reading taken once a pass; as one, it is also accepted as an E-stop
  contact or a sense input (an analog one is still refused). This agent reads no expander or Modbus input yet, so
  such a port reads unknown, and the E-stop takes unknown as pressed. On a language-1 node an input whose variable
  states no `debounceMs` takes the port entry's (nodes/README.md §1: a variable-level value overrides it).
- **The golden vectors with their new expectations**: every GPIO input of the nine v3 bundles the vectors carry
  (two-node-layout, instruction-switch, rule-edit-swap, rewire-swap) is requested on the fake chip with the bias its
  entry states, and every safety input states one. The runtime vectors replay unchanged.

Owed elsewhere, seen while checking (not changed here): the backend bundles the integration check emitted carry no
`pull` on the mains and UPS sense (site-north DI2 / DI3, stated `down` since this amendment), so on those bundles this
agent pulls them **up**: a broken wire to a driven, active-high sense module would then read as supply present. The
backend must copy the port's `pull` and `debounceMs` (SYNC.md v1.15.0 7(g)) before a v3 bundle reaches a node wired
that way. The integration check's `hw_check.py` fake opener takes no `bias`, so on this agent it now fails safe (the
E-stop latches); with an opener that takes one, every line request carries its bias.

Not covered — contract gaps reported with this version: a v3 card's live values (on/off, current) have no message of
their own (the v1 `variables` message carried AAB variables, and v3 has none); data cards (§4b) are not built in
this agent; how a `setPower` names a valve card in v3 (`valve.open` here); a block suspended by the backend reaches
the node as a recompile, so its switch is announced with cause `confirmed`; port ids (DI3) are not in the bundle,
so refusals name the card and its port; `three_or_two_phase` motors are judged as three phase; an AI phase-sense
input counts as present when non-zero; a bias that could not be applied is in the log only (the heartbeat has no
field for it); an AI port as a power or supply sense has no presence threshold; `instruction_switched.cause` has no
value for a part change when the clock comes good on another date (the node switches without the event); the
golden vectors fix the boot pass's writes, so a cold load cannot write the latching coils off (the migration from language 1 does). The bench page and
its physics (`blocks.simulated_io`) and the simulator CLI still drive language-1 bundles only.

Found on the way, not changed: `--offline` still posts the one-shot hardware report and the program ack over HTTPS,
and the default ingress URL is the production one.

121 tests in `tests/test_v3_*.py` (a fake I/O, a SimClock, no hardware): the golden vectors of
`contracts/programs/vectors` run through the runtime in 1 s passes — `instruction-switch.json` (adding Valve 2 while
Valve 1 runs: the motor and Valve 1 never stop), `rule-edit-swap.json`, `rewire-swap.json` — with both bundles of
`two-node-layout.json`, the cross-node cases of `cross-node-refused.json` and the field box side of the rewire; one
test file per built-in; and every event the runtime sends validated against `event.schema.json` with AJV (when
`AJV_PATH` is set). `main()` was driven offline with a persisted v3 `program.json`.

After the merge with 0.3.4: 944 tests in the whole suite (`tests/test_v3_on_0_3_4.py`, 7, and
`tests/test_v1_parity.py`, 4, are new), no hardware. After the review of 2026-10-17: 970; after the second: 977.

## 0.3.4 (2026-10-16)

**Security — any board's config could command every other node; now a node carries out only
commands signed for it (contracts v1.14.2, `Agal/contracts/nodes/command-signing.md`; founder
2026-10-15: yes).** Every node connects to HiveMQ with one shared device login, written into every
`config.yaml`, so whoever held one could publish to any node's `agal/{nodeUid}/commands` — the
topic name was the only separation. The backend is to sign every message it publishes for one node
with that node's `authToken` (`sigAlg` hs256-v1, `sigTs`, `sig` = base64url HMAC-SHA256 of the
canonical JSON of the rest — SYNC.md v1.14.2 item 3, not built when this was written), and from
0.3.4 the node checks every message at the MQTT boundary,
before anything reads it (`command_signing.py`; `mqtt_client._on_message` hands the handler the
very object it verified):

- **refused, in the reference's order** (a port of `nodes/reference/signing.mjs`): no `sig` →
  `unsigned`; a `sig` that is not 43 base64url characters, another `sigAlg`, a `sigTs` that is not
  a safe integer, no usable `commandId`, no canonical form, or a MAC that does not match
  (`hmac.compare_digest`) → `bad_signature`; an id accepted before → `replayed`; below the replay
  floor, no trusted now, or signed more than 10 minutes before / 2 minutes after the node's trusted
  now → `stale` (review, below: the node's now is the backend's time from its HTTPS answers, else an
  NTP / RTC clock — the time check is never skipped). A refused copy never burns an id: a forged
  copy carrying a real command's id does not stop the real one.
- **canonical JSON as JavaScript writes it**: keys sorted by UTF-16 code units at every level,
  `JSON.stringify`'s string escapes (Tamil, ₹, U+2028 and emoji raw), and every number as
  ECMAScript writes the double it parses to (`1.0` → `1`, `1E-7` → `1e-7`, `1e21` → `1e+21`,
  `2^53+1` → `9007199254740992`) — `json.dumps` does none of that. The parser refuses `NaN` /
  `Infinity`, as `JSON.parse` does.
- **the replay set survives a restart**: every accepted id with its `sigTs`, aged 7 days on the
  backend's clock (`highWater`), at most 10,000 (the oldest leaves and raises `evictedFloor`),
  in the state directory (`command_replay.json` + `command_replay.journal`; one fsynced line per
  command, written BEFORE it is carried out, folded into the snapshot every 2,000 and at start; a
  torn last line is ignored, a damaged snapshot is kept aside and the set starts empty, logged).
  So a Pi that has not synced NTP yet still refuses a replay. If the card cannot be written the
  command is still carried out (remembered in memory, logged as an error) — refusing would leave
  the node unable to take the command that switches the pump off.
- **every refusal is logged** (reason, commandId, why — never the token; the type only when the
  MAC verified). A `bad_signature` or `stale` refusal of a 20-character auto-id is answered with a
  failed command ack carrying `reason` and `error` "refused: <reason> — …" through the 0.3.3 HTTPS
  ack queue — at most 5 `bad_signature` and 20 `stale` answers in any 10 minutes, counted apart (a
  flood of forged messages must not spend the node's 1000-an-hour ingress budget, nor crowd out a
  real command's answer); `unsigned` and `replayed` are only logged (review); a prefixed id
  (`sync_…`, `scan_bus_…`, `test_port_…`) has no document and is only logged. A refusal ack never
  supersedes the real command's own `acknowledged` in the queue. A `firmwareUpdate` refused
  `stale` by the time check (its MAC verified, so its fields are the backend's) is also reported as
  `firmware_update {phase: failed, commandId, toVersion, rolloutId, error}`, once per command, so
  `otaStatus` does not stay `updating` — not one below the replay floor (a replayed capture,
  review); its ack stays on MQTT, as every firmwareUpdate ack does (its id is a `/nodeCommands`
  doc, which commandAck does not track).
- **a node with an empty `auth_token` carries out nothing** from its command topic
  (command-signing.md §5.5, review): it can check nothing, every message is refused and logged — at
  start and at most once a minute — "no token: join this node again". Every node made by
  `createNode`, the join command or `provision` has a token; the backend never publishes to a node
  it holds none for.

**Release order: never put 0.3.4 on a node before the backend that signs is deployed** — a 0.3.4
node refuses every unsigned command, `firmwareUpdate` included, and answers each tracked one
`failed` "refused: unsigned"; it works again by itself once the signing backend is out. Agents
0.3.1 / 0.3.3 ignore the three extra fields.

**Fix — no node alert reached the cloud.** A node's events — the protection trips
(`sensors/protection.py`: dry-run cut-off, low water, baseline learned), the block runtime's
(`blocks/cloud.py` `CloudSink.event`: plot / schedule runs, commands applied or failed, outputs
released, runs cut short …) and `buffer_overflow` — were published on the MQTT status topic only,
which nothing consumes, in a flat body the ingress answers 400 (contracts v1.14.1 review, SYNC.md
3e). Now every `publish_event` also goes to `node_events.NodeEvents` (`main.py` wires it as the
MQTT client's event sink, like the ack sink), which posts `{type: "sensor_event", payload:
{nodeUid, event, timestamp, eventId, timestampUncertain?}}` (functions.yaml 1.10.2
`SensorEventPayload`) with the node's Bearer token, connected or not:

- `nodeUid` is `config.node.uid`, not `mqtt.username` (the shared login's name on a node that uses
  it — what the MQTT copy has always carried);
- `eventId` (20 random characters) is minted when the event is raised and is the same on every
  retry: the backend files `/events/{nodeUid}_{eventId}` once, so a retry rings no phone twice;
- `timestampUncertain: true` while the clock is not trusted (the backend files it at receive time);
- in order, one at a time: not delivered (unreachable, 408, 429, 5xx) → tried again after a
  back-off (1 s … 60 s; 60 s after a 429) while the events behind it wait, for up to 24 h; any
  other 4xx → given up at once, logged; events have their own budget of 300 an hour (burst 30)
  within the node's 1000-an-hour ingress limit, and an event over it waits; at most 256 waiting (the oldest that is not a protection event
  leaves first, logged); NaN / Infinity in a payload become null; one more try each on a clean
  stop, after the runtime's own stop events.

The MQTT copy is kept (harmless; whether to drop it is the founder's, SYNC.md v1.14.2 item 6).
Events are not persisted: a restart while the ingress is unreachable loses the ones waiting.

Verified (signing + events): `tests/test_command_signing.py` (179) — the 13 signing vectors (canonical
text and sig) and all 31 verifying sequences of `Agal/contracts/nodes/vectors/command-signing.json`,
each against the in-memory set and the persisted one; eviction at 10,000 (and after a restart);
a message nested past Python's recursion limit refused, not a crash; 35 number literals against
the text Node's `String()` gives for each; escapes; UTF-16 key order; 300 random nested
messages canonicalised and MACed by the reference `signing.mjs` in Node (run when `node` is on the
PATH); the replay set across restarts, its floor, aging, a torn journal line, a damaged snapshot,
the fold, an unwritable card, the id on disk before the handler runs; the gate at the MQTT
boundary (every reason, the clock rule, untracked ids, non-commands, the 20-in-10-minutes cap,
the stale firmwareUpdate report, no token); the refusal ack's HTTPS body; main() wiring.
`tests/test_node_events.py` (20) — the wire body and Bearer token, the node's uid, retries with
one eventId, order, 4xx, 429, the deadline, the cap, the budget, the stop, each source of events,
main() wiring. Mutation-checked: Python's key order, `repr` numbers, no clock check, no floor, a
refused copy remembered, no MAC compare, no persisted line, no cap, the broker username as
nodeUid, no event sink, the gate not wired, uppercase `\u` escapes — each fails the tests. Not run
on a Pi, against the broker, or against a backend that signs (not built yet).

**Review fixes (contracts v1.14.2 review, same release).** Checked against the code; each held:

- **No trusted now → `stale`; the node's now is the backend's** (`server_time.py`, command-signing.md
  §5.4). As first written, the time check was skipped while `clock_ok()` was false — every boot of a
  Pi until NTP syncs — so a signed command captured from the topic and never carried out (sent while
  the node was offline, or before it ran 0.3.4) stayed executable for 7 days, and after a fresh or
  damaged replay set for ever; a `setPower` ON that timed out hours earlier could run at the next
  boot. Now `HttpReporter` hands the `Date` of every HTTPS answer from the backend (telemetryIngress,
  getProgram, reportHardware, getBootState — `https://` only, so TLS authenticates it; error answers
  too) to a `ServerTime`, and the gate's now is that plus the monotonic time since; before the first
  answer, the wall clock when `clock_ok()`; otherwise none, and every command is refused `stale` "no
  trusted time yet" (it burns no id; the answer to its own refusal ack gives the node the time). The
  backend's time comes first even with NTP: a Pi 5 has `/dev/rtc0` but no battery. Node events take
  the backend's time too when the clock is not trusted (`timestampUncertain` only when neither is).
- **`clock_ok()` probes on its first call** (`blocks/clock.py`): the last probe started at 0.0 on a
  monotonic clock that counts from boot, so for the first minute after boot it said "trusted"
  without asking — the minute right after a power cut. `/dev/rtc0` still counts as an RTC (the block
  runtime's schedule hold depends on it on DS3231 kits); the gate no longer relies on it once the
  backend has answered.
- **No token → nothing carried out** (above). As first written a tokenless node carried out
  everything — while the backend's `provision` / `getProvisionScript` wrote `auth_token: ""` for every
  joined node (fixed there too).
- **`unsigned` / `replayed` refusals are not posted; `bad_signature` (5) and `stale` (20) budgets
  apart** (above): junk with made-up 20-character ids no longer spends the answers a real refusal needs.
- **A replayed old `firmwareUpdate` is not reported as this node's update failing** (above): one below
  the replay floor overwrote `otaStatus` and a finished rollout's record (the backend now also takes a
  `firmware_update` only with the token and for the node's current update).

Verified: `tests/test_command_signing.py` (now with vectors 1.1.0 — 33 verifying sequences, a step
may carry its own `clockTrusted` / `nowMs`), `tests/test_server_time.py` (13: the Date reference,
http and unreadable Dates ignored, the order backend → trusted wall clock → none, every reporter
call and an error answer feed it, the first probe, node events). Full suite 685 passed.

**Join safety — the one last join that takes the Pi to 0.3.4 (contracts v1.14.2 join safety,
`Agal/contracts/nodes/README.md` §10, `command-signing.md` §7; SYNC.md v1.14.2 item 8 c).** The
only production node (a Raspberry Pi 3B on agent 0.3.1, joined by the join command, no git) gets
0.3.4 by one last join; after it every update is over the air. A compatibility check of that join
(2026-10-15) found three hazards; the agent's part of each:

- **H3 — no placeholder version, ever.** On every clean stop (the join's `systemctl restart`, an
  update's restart, a shutdown) `heartbeat.stop()` posted the offline status without a version, so
  `HttpReporter.report_status` / `AgalOneMqttClient.publish_status` filled in their default
  `"0.1.0"`, and the backend stored it: the app showed the node on agent 0.1.0 during every restart.
  The online heartbeat also fell back to `"0.1.0"` whenever `heartbeat._get_agent_version()` could
  not read the installed package's metadata, and otherwise re-read that metadata on every beat —
  which names the NEW version as soon as pip has replaced the files (a join; an update waiting up to
  30 min for a pump to stop) while the old process still runs. Now every status — online and
  offline, HTTPS and MQTT, and the MQTT last will — carries `agal_one_agent.__version__`, the
  version of the code that is running, resolved once when the package is imported
  (`HeartbeatPublisher(agent_version=…)`, main.py passes it); `_get_agent_version()` is gone; the two
  reporters take `firmware_version: Optional[str] = None` and send no `firmwareVersion` without one.
  The offline status goes over HTTPS even when the MQTT publish raises (each channel on its own;
  the online heartbeat too). The backend's half (an offline status never writes the version, an
  online one never `0.1.0`) is separate; the last join's own restart is agent 0.3.1's clean stop,
  which still says 0.1.0 — only the backend's half covers that one.
- **H1 — the agent's side of the guard.** The served join script refused the board's own config as
  "already linked to another Agal One node": it looked for `uid: "<nodeUid>"`, and `update_pins`
  (identical in 0.3.1 and 0.3.4) rewrites `config.yaml` with `yaml.dump` on every `syncPinConfig` —
  every card link and unlink — which writes the uid bare, or in single quotes when it would read as
  another type. The new guard is the backend's (contracts §10.1); nothing in the agent changes for
  it. Checked here: the vectors' `guard.script` (bash) proceeds on the `config.yaml` that
  `update_pins` writes from the join's own file, for 14 node ids (among them every kind `yaml.dump`
  single-quotes: hex, decimal, octal, a date, true / No / null, `1_000`, `-1`, a 128-character id),
  refuses another node's and a near one, and the old guard refuses it (H1 reproduced); the file has
  one `uid:` line; nothing else in the agent reads the uid as text — `AgentConfig.from_yaml`
  (`yaml.safe_load`; main, the OTA reports, balena's `render_config.py` with `safe_dump`) reads back
  the same `str` uid, token, topics and pins, after one rewrite and after several.
- **H2 — the token at boot, and a command signed just before the promotion.** The backend's half
  (the join fetch writes a PENDING token; the board's first request with it promotes it) needs no
  agent change: the agent holds one token, its `config.yaml`. Checked here, with `main()` run as
  systemd starts it after the join's restart (urlopen and the broker faked): the first HTTPS request
  is the hardware report to `reportHardware` (a door that reads the credential fresh), from a thread
  started before MQTT, with `Authorization: Bearer <config token>` — 8 ms after start on the
  development Mac, with the broker never answering (on a board the inventory before it includes an
  I2C read of 112 addresses per bus — not measured on the Pi; a bus that hangs delays it, and then
  the first heartbeat, posted as soon as MQTT is up, is the first request); every request at boot
  (hardware report, heartbeat, the legacy
  `getBootState` — its token in the body too —, the clean stop) carries that token. A command the
  backend signed with the OLD token in the seconds before the promotion is refused `bad_signature`
  (never carried out) and answered with a failed ack carrying `reason: bad_signature` — a 20-character
  auto-id, i.e. every app command; at most 5 such answers in 10 minutes — and that answer is posted
  with the NEW token, so it promotes the pending token and is then handled: the command shows
  `failed` (refusal `bad_signature`) instead of waiting for its timeout; a 503 (the promotion could
  not commit) is tried again until it lands; the next command, signed with the new token, is carried
  out. A prefixed node poke (`sync_…`, `scan_bus_…`, `test_port_…`) refused in that window is only
  logged (it has no document); what it would have started the agent does at every start anyway (the
  hardware report, the program pull). Commands published while no agent is subscribed (the restart
  itself) are not delivered (clean MQTT session) and time out in the app, as before.

- **`config.yaml` is never half written** (SYNC.md v1.14.2 item 8 f — listed there as found and not
  fixed; beyond the contract's agent item c). `update_pins` opened `config.yaml` for writing and
  streamed `yaml.dump` into it, so a power cut or an error mid-write left it truncated: the agent
  cannot start on it — no over-the-air update can reach a board then — and the next join's guard
  refuses it, i.e. the terminal again; and 0.3.4 is the last version a terminal installs. Now the
  whole text is dumped first, written to `.config.yaml.pins-*` in the same directory (mode 600, then
  the old file's mode — it holds the token), fsynced, renamed over `config.yaml`, and the directory
  fsynced: a power cut leaves the old file or the new one. A temporary file left by a cut-off write
  is removed by the next one (it is not the join's `config.yaml.new`). Where the rename is refused
  (a `config.yaml` bind-mounted on its own) it writes in place, logged. Tests (4): an error halfway
  leaves the file byte for byte, nothing left behind, the pins still applied in memory; a cut between
  the write and the rename leaves the old file, the next write cleans up and the guard proceeds; a
  new inode with mode 600 / 640 kept; the in-place fallback. Mutation-checked: the old in-place
  write, the mode not carried over, no clean-up — each fails them.

Verified: `tests/test_join_safety.py` (72) — the 12 status vectors through a port of the contract's
`storedFirmwareVersion` (then applied to what this agent sends: its heartbeat stores
`__version__`, its clean stop leaves it); `__version__` matches the iot-node pattern and is not
`0.1.0`; the `__init__.py` fallback = setup.py's version; stop() on both channels; the metadata
changed or unreadable mid-run; no default; the MQTT failure; the last will; all 21 guard vectors
through bash; the update_pins round trip for 14 ids; nothing else reads the uid as text; the first
request at boot with no broker; a full boot to the main loop and a clean stop; a command signed with
the old token before the promotion, with and without a 503 (and 4 more for the config write, above:
76 in all). Mutation-checked, each fails a test: the
0.3.3 heartbeat and reporters, stop() without a version, the `0.1.0` default back, the heartbeat
re-reading the metadata (either channel), no per-channel try in stop(), the hardware report moved
after MQTT, the reporter without its token, `bad_signature` not answered. Full suite 761 passed. Not
run on a Pi, against the broker, or against a backend with the pending credential (not built yet).
Found, not fixed: in a development venv with a stale editable install `__version__` is that install's
metadata, not the checkout's (this worktree's tests run as 0.2.1) — on a board the join and the
over-the-air update install the archive with its metadata, so they agree; the MQTT copies of every
status still name `mqtt.username` (the shared login on such a node) as `nodeUid` — nothing consumes
them; `getBootState` is posted to the fixed production URL whatever `telemetry.ingress_url` says.

**Join safety, the review (2026-10-15; contracts v1.14.2 join-safety review, vectors `join.json`
1.1.0; README §10.1).** The guard as first built read every `uid:` line in `config.yaml`, and
`update_pins` copies what the cloud sends verbatim before `yaml.dump`: a pin label with a newline, a
label `yaml.dump` folds at 80 columns right before `uid: …`, a `sensor_params` or `protection` key
named `uid`, or a node name over several lines put more `uid:` lines in the file (indented deeper than
the node block's keys), and the guard took them for another node's — the board's own last join refused
again, the terminal again. The contract's guard now reads only the `uid` keys of the top-level `node:`
block at its own indentation (awk + grep). Nothing in the agent changes for it; tests added
(`tests/test_join_safety.py`, 97 now): for each of those four pin kinds and two ids (one `yaml.dump`
single-quotes) the file `update_pins` writes has the extra `uid:` lines, the vectors' guard proceeds
for this node and refuses another, the first guard refused it (control), and `from_yaml` reads back the
same uid, token, labels and parameters; the same for a node name over several lines; and the 33 guard
vectors (21 before) through bash. Agent 0.3.1's `update_pins` is the same code, so the same files come
from the production Pi.

**Fix — a firmware update needed the board's terminal (founder, 2026-10-15: "once the rpi is
linked, there should not be a need to do anything on the pi terminal especially the updates").**
The app's Update firmware button (`updateNodeFirmware` → `/nodeCommands` → `firmwareUpdate
{version}`) made the agent `pip install git+https://github.com/logapradeep/agal-one-agent.git@vX.Y.Z`.
The joined boards have no git (the join script installs none; the Pi 3B on Debian 13 has none), so
every update failed and every upgrade so far was a fresh join command typed on the Pi. From 0.3.4
(`ota_updater.py`):

- **the release archive, no git.** A `firmwareUpdate` may carry `source` + `sha256` — the same
  pair `/config/agent` pins for the join script. The agent accepts only
  `https://github.com/logapradeep/agal-one-agent/archive/refs/tags/<the target tag>.tar.gz`
  (http, another owner or host, another tag, a branch, a query, a zip, a file: URL are refused),
  and only with a sha256. It downloads the archive over HTTPS into
  `/var/lib/agal-one-agent/ota/download/` (32 MB cap, 30 s per read, 300 s in all, redirects only
  to github.com / codeload.github.com over HTTPS), checks the sha256 BEFORE installing (a mismatch
  ends `failed`, nothing installed, the file deleted), and installs it the way the join script
  does: one `agal-one-agent @ file:///… --hash=sha256:…` line, `pip install --no-deps
  --require-hashes -r` (pip checks the hash a second time). If `pip check` then says the new
  version needs a library the board lacks, that library is installed from PyPI the way the join
  script installs the agent's libraries — only a MISSING one, and only after pip's dry run
  (`pip install --dry-run --report`) shows it changes no library the board has. A release that
  needs an installed library at another version (directly, or through the library it adds) is not
  installed: the update is undone with the libraries untouched (see the review fixes below). The
  installed version must be the target, or the update is undone. The archive is deleted after the
  install.
- **the way back needs no network.** Before installing, the agent saves the running version to
  `/var/lib/agal-one-agent/ota/previous/`: every file its pip RECORD lists — the `agal_one_agent`
  package, its `agal_one_agent-X.Y.Z.dist-info` and its console scripts in `venv/bin` — each with
  its sha256 (~1–2 MB). This works for the first update from a joined 0.3.x too: no cached archive
  of the old version is needed. Putting it back (on a pip failure, and in the verify-timer's
  rollback) checks every saved file first, removes the files of whatever agal-one-agent is
  installed (its RECORD, the package directory, every agal_one_agent-*.dist-info), and copies the
  saved files back. Libraries are not part of it, which is why an update never changes one the
  board has (it may add a missing one — harmless after a rollback). An install with no RECORD (an
  editable `pip install -e .`, as `scripts/install.sh` does) cannot be saved, so it is not updated
  over the air.
- **the verify-timer, on every board.** The join script installs only `agal-one-agent.service`, so
  the joined boards never had the independent rollback check. Before an update the agent writes
  `agal-one-agent-ota-verify.service` / `.timer` (where missing or different), `daemon-reload`s
  and `enable --now`s the timer; if that fails the update does not go ahead. The service runs
  `venv/bin/python -I /var/lib/agal-one-agent/ota/verify.py`, which puts the SAVED version first
  on the import path: the rollback is done by the code that wrote the update's state, so a new
  version that does not even import is still rolled back. `systemctl` is called directly when the
  agent is root (it is, under the join script's unit), `sudo -n` otherwise.
- **not while a motor runs.** A restart switches every output off. Before downloading, the update
  waits while anything the node drives is ON — every program output at an on level and every plot
  run (`BlockRuntime.outputs_on`), and the legacy GPIO / PWM outputs (`gpio_handler` /
  `pwm_handler.outputs_on`) — checking every 10 s for up to 30 min
  (`AGAL_ONE_AGENT_OTA_BUSY_WAIT_S`). Still on: `failed` with an error starting `busy:` that
  names them; nothing downloaded. Something switched on while pip ran: the restart waits again
  (bounded the same way; the verify-timer's deadline kept ahead meanwhile), else the saved version
  goes back and the update ends `busy:`. Outputs that cannot be read count as on. Chosen over
  "defer and let the cloud retry" because the backend has no retry for a node command, and
  `OtaStatus` has no deferred value (contract gap: listed for Agal/contracts). A port test's pulse
  (≤ 5 s) is not waited on.
- **the update runs on its own thread** (`main.start_firmware_update`). It ran inside the MQTT
  callback, so for the whole pip install no command reached the node — now the command that
  switches the pump off arrives while the update waits for it. A second `firmwareUpdate` while one
  runs is ignored (logged).
- **git only for an older backend.** A command without `source` installs from git (`--no-deps
  --force-reinstall`, then only missing libraries, as the archive), logs that this needs git, and —
  on a board without git — ends `failed` at once saying the backend has to send the archive. It
  gets the saved version and the verify-timer too.
- **honest outcomes.** An update the agent was waiting on or downloading when it stopped is
  reported `failed` ("interrupted") at the next boot, instead of the app showing "updating" for
  ever. A probation that finds the OLD version running (the agent restarted while pip ran) reports
  `failed`, not `succeeded`. The events go to the config's `ingress_url` with the node's token.
  pip's `ERROR:` lines, not a traceback, are the event's error.

Unchanged: `firmware_update_started/succeeded/rolled_back/failed` over HTTPS, and a state written
by an older agent (no saved version) rolled back through git as before. The probation, the verify
deadline and what `run_verify` does on a failed rollback changed in the review fixes below.

**Review fixes (2026-10-15, same unreleased 0.3.4).**

- **a power cut during the install no longer bricks the node.** Nothing was flushed: on ext4 a
  file created and renamed less than ~30 s before a power cut can come back empty while the rename
  survives, and pip runs 30–90 s right after the snapshot. Now the snapshot is `os.sync()`ed before
  it is renamed into place (and its directory fsynced), `ota_state.json` and the verify units are
  written fsync + rename + directory fsync, and the board syncs after writing `pending_verify`
  (before pip), before the restart into the new version and after a rollback.
- **a failed rollback is tried again, never given up.** It used to end `failed` for good (the
  timer acts only on `pending_verify`), with a crash-looping venv left for the terminal. Now the
  state stays `pending_verify` with `rollbackAttempts` and a backoff (1, 2, 5, 10, then every
  30 min); the failure is reported once (`firmware_update_failed`, "… the board keeps trying"). The
  way back is the saved copy, and — when a saved file is damaged, or what it put back does not
  start — the previous version's release archive with the sha256 this board recorded for it
  (`ota/installed.json`, written when an update is confirmed or rolled back, or the join script's
  `/opt/agal-one-agent/agent.requirements`), downloaded, hash-checked and installed `--no-deps
  --require-hashes --force-reinstall`. Either way the restored agent must import in a fresh
  interpreter (`venv/bin/python -I -c 'import agal_one_agent.main'`) and be the previous version
  before it is called `rolled_back`. When the update's own process could not put the files back,
  the state is left to the verify-timer the same way (it no longer ends `failed` with a half venv),
  and once the files are back that running process is not restarted (it already runs that version).
  The verify launcher now hash-checks the saved copy before it runs the saved version's rollback
  code (else the installed agent's), and imports nothing at all unless an update is in probation.
- **updates only move forward, and never below 0.3.4.** The command's sha256 proves only that the
  archive matches the message, and every board shares one MQTT login (ADR-012: topics apart by
  convention), so anyone holding a config could send any old tag. The agent now refuses a target
  older than `MIN_OTA_VERSION` = v0.3.4 (the first that updates from the archive: anything older
  would need git, and the terminal, for its next update — and 0.3.1 and older pulse an active-low
  output on at boot) and a target older than the running version ("a newer release is how a bad
  one is undone"). The backend refuses the button when the pin is older than the node's
  `firmwareVersion`. Binding the hash to the backend (the agent confirming the command over its
  own HTTPS token) needs a contract change first — listed, not built.
- **the new process owns its probation.** It was one check, 60 s after the boot reconcile and the
  program pull, passing only if MQTT was connected at that instant, against a deadline written
  before the old process even stopped; now that the agent installs the verify-timer, that rollback
  is live on every board. Now `begin_probation()` at boot, before MQTT, counts the deadline from
  that boot (`AGAL_ONE_AGENT_OTA_PROBATION_DEADLINE_S`, 300 s; once per update, so a version that
  crash-loops after that point is still rolled back); from 60 s on the new process checks every
  5 s and confirms at the first healthy check (connected and the main loop alive), as long as the
  update is pending. Its main loop writes `ota/heartbeat.json` (version, connected, what is ON)
  every 10 s during probation only; past the deadline the verify-timer holds the rollback while
  that heartbeat is fresh, names the target and says an output is ON (up to 30 min — a rollback
  restart would cut a running pump), or says it is connected (up to 2 min, it confirms itself);
  a stale heartbeat or one naming another version does not hold it. The verify-timer and the
  confirmation take a lock on the state (`ota.lock`), so one cannot land inside the other; a new
  firmwareUpdate while an update is still pending is ignored (its state is left alone).
- **the verify-timer's restart reaches systemd.** It was a detached `systemctl restart` from a
  oneshot unit, killed with the unit's processes when the check exited, maybe before it asked;
  now `systemctl restart --no-block` run to the end (it returns once the job is queued).
- **an active-low output on a Broadcom number is not "on" when it is off.** `gpio_handler` kept
  the pin's level, and the block runtime writes an active-low port inverted — an OFF relay held
  HIGH read as ON, so every update waited 30 min and ended `busy:`. It now keeps ON/OFF
  (`GpioHandler.write(..., logical=)`, passed by `blocks/io.HardwareIO`).
- **the old process loads nothing from the new version.** Every module of the running agent is
  imported before pip runs, so a handler or lazy import first used while the update waits (up to
  30 min, with a pump on) is the old version's, not a new file next to old ones.
- **the boot clean-up of an interrupted update runs before MQTT connects**, so it can no longer
  report a live download `failed` and delete it; it also leaves alone an update this process runs.

Backend, same wave (agal-one `fix/2026-10-ota-archive`): the join script resets
`ota_state.json` to idle and removes the saved copy before it installs the agent (an old
probation's timer cannot roll a fresh join back), and `uninstall.sh` stops and removes the
verify-timer the agent installs.

Limit, on purpose: a release that raises a library's minimum (paho-mqtt>=3, say) cannot come over
the air — the saved copy covers the agent's own files only. Keep library minimums in releases, or
extend the saved copy to the libraries pip's dry run names before such a release.

Verified (review fixes): 95 tests in `tests/test_ota_rollback.py` (was 61), and with real pip
26.2.1 on Python 3.14 in a scratch venv (local archives standing in for GitHub's; the download,
systemctl and the event post faked): joined 0.3.4 → 0.3.5 through the archive, its probation
confirmed by `Probation.check()` in a fresh 0.3.5 process (archive recorded in `installed.json`);
0.3.6 needing `requests>=2.32` refused because pip's dry run said it would also change
charset-normalizer 1.4.1 → 3.5.2 (0.3.5 back, no library changed); 0.3.8 needing paho-mqtt>=3.0
refused from pip check's "has requirement" (paho stayed 2.1.0); 0.3.7 adding `six` installed it
and nothing else; then, with the saved `main.py` emptied and the deadline past, `run_verify`
installed 0.3.5 again from its archive (`--force-reinstall`), a fresh interpreter imported 0.3.5,
`pip check` clean, `rolled_back`, restart queued `--no-block`. Not run on a Pi, not against GitHub,
no real power cut or systemd.

**What it does not fix: the board on 0.3.1 / 0.3.3 needs the join command ONE more time.** Those
versions' own updater is the git one (it runs the update, not the new version), so the step to
0.3.4 cannot come over the air on a board without git. From 0.3.4 on, every update can. For the
button to send the archive the backend has to put `source` + `sha256` (from `/config/agent`) on the
`firmwareUpdate` command — `onNodeCommandCreated` sends only `{commandId, type, version, rolloutId}`
today; until it does, 0.3.4 answers an update with "this board has no git … the backend has to send
them".

Verified: 61 tests in `tests/test_ota_rollback.py` (was 9), no network — a fake downloader, pip,
systemctl and clock, against a real venv tree in tmp_path so the save and the restore run for
real; the verify launcher is run in a subprocess and is shown to run the saved version's code.
And with real pip 26.0.1 in a scratch venv (Python 3.9; local archives standing in for GitHub's,
the download, systemctl, the restart and the event post faked), on this code: joined as 0.3.4
with the join script's flags; updated to 0.3.5 through the hashed requirement line; `run_verify`
past the deadline put 0.3.4 back with no pip call and no download — file for file, one
dist-info, `pip check` clean, a fresh interpreter imports 0.3.4; an unbuildable 0.3.6 failed in
pip ("ERROR: Failed to build 'agal-one-agent' …") and 0.3.4 came back the same way, no restart.
An earlier run of the same, on a venv joined without the libraries, also installed paho-mqtt,
PyYAML and python-periphery after `pip check` named them. Not run on a Pi, not against GitHub's
real archive and redirect, systemd and the busy wait not exercised on a board.

## 0.3.3 (2026-10-15)

A new version rather than more of 0.3.2: 0.3.2 (cb03e4b) is already on `main` as its own
build, and the OTA path tells builds apart by their version string. Neither is tagged;
`/config/agent` still pins 0.3.1.

**Fix — every command ended `timeout` although the node switched (seen in prod 2026-10-15).**
The backend learns what became of a command only from `telemetryIngress` over HTTPS
(`commandAck` → `handleCommandAck`); nothing consumes the MQTT status topic (Cloud Functions
cannot subscribe). The agent acked over MQTT only — `HttpReporter.report_command_ack` existed
and nothing called it — so `checkCommandTimeouts` marked every `/assetCommands` doc `timeout`
a minute after it was queued. Now (`command_acks.py`):

- every ack, from every call site (`command_executor.py`, `main.py`, `ports/links.py`), still
  goes through `AgalOneMqttClient.publish_command_ack`, which hands it to its **ack sink** — a
  `CommandAcks`, given to it by `wire_command_acks` in `main.py` — whether MQTT is connected or
  not, and still publishes it on the status topic when connected (harmless; a future broker
  consumer may use it);
- `CommandAcks` posts from its own thread through `report_command_ack` (`{type: "commandAck",
  payload: {nodeUid, commandId, status, appliedValue?, error?}}`, with the node's
  `Authorization: Bearer`). A command is carried out, and the MQTT loop goes on, while its ack
  is on the way;
- **what is posted** — the ingress allows 1000 requests per node per hour, shared with
  heartbeats, telemetry, card variables and alerts, so a command costs at most two posts: the
  final `completed` / `failed` always; `acknowledged` after a 0.5 s hold, unless a later ack of
  the command is queued by then. A switch that completes at once costs one post; a command that
  takes longer two, and its `acknowledged` tells the backend the node has it (`acknowledgedAt`)
  — it does not keep the command from timing out: the backend's sweep ends `acknowledged` /
  `executing` 60 s after the command was created too, and a final ack after that still lands.
  `executing` after an `acknowledged` tells the backend nothing new and stays on MQTT; without
  one it is posted;
- **only acks the backend can use**: ids it keeps no command document for stay on MQTT —
  `boot_<assetId>` (boot reconciliation, as before) and the node-level pokes it mints with a
  prefix, `sync_…` (pin config, program, port table), `scan_bus_…` and `test_port_…` (their
  results come back as `port_test` / `bus_scan` / `programAck`; the backend answers their acks
  "untracked"). A port test or a bus scan cost two posts for nothing. The OTA `firmwareUpdate`
  receipt and its failure stay on MQTT too (`publish_command_ack(..., ingress=False)`,
  `main.handle_firmware_update`): the outcome comes back as `firmware_update` events, and the id
  names a `/nodeCommands` doc, which commandAck does not track (each post cost the backend three
  reads and a second's wait for a doc that is never there);
- **in order**: an ack waits while an earlier ack of its command is queued, and an intermediate
  ack with a later one queued behind it is not posted. Final acks also keep their order across
  commands: when one is to be tried again (not reached, 408, 429, 5xx) every ack waits out its
  back-off. Before, only "not reached" and 429 paused the queue, so with two quick switches (ON,
  then OFF) an ON `completed` that met one 503 was posted after the OFF `completed`, and the
  backend echoed "off" then "on" — the card showing ON with the relay off (reproduced with the
  real `CommandAcks` and a fake ingress answering 500 once; now ON, ON, OFF). An intermediate
  ack tried again waits alone;
- **retries**: when the ingress is not reached or answers 408, 429 or 5xx, a final ack stays
  queued and is retried with back-off (1 s doubling to 30 s; 60 s after a 429) for 5 minutes,
  then given up with an error in the log (the command stays `timeout`); an intermediate ack
  gets 3 tries. Any other 4xx is never going to be accepted and is not retried (the ingress
  answers a commandAck without the node's verified token 401). On a clean stop each waiting ack
  gets one more try, for 3 s at most;
- **bounded**: an `acknowledged` / `executing` that arrives while 32 acks wait is not queued; a
  final ack always is, up to 256 — there the oldest waiting intermediate ack makes room, and only
  when every waiting ack is final is the oldest given up, logged.

`report_command_ack` now returns the HTTP status (None when the ingress was not reached), so a
refusal is told from an outage. 37 tests in `tests/test_command_acks.py`, with a fake reporter
and no network — including that `main()` calls `wire_command_acks` (the missing call was the
bug) and that a wired client's ack reaches `HttpReporter`'s post. `main()` was also driven with
a fake broker and a fake ingress: a `setPower` posted one `commandAck` `completed` with
`appliedValue` 1 and the node's token, with MQTT up and with MQTT down, and the MQTT callback
returned in under 5 ms.

**The backend half** (agal-one, branch `fix/2026-10-command-acks`, not deployed): the ingress
takes a `commandAck` only with the node's verified token and only from the command's own node,
keeps final statuses final (a completion after the backend's own `timeout`, or after a `failed`
it wrote itself on a publish error, still lands), and echoes a completed command's value onto
the card only when no newer command for the same card and port exists. A backend without that
change (agal-one `60c3dfd` and earlier) takes an agent 0.3.3 ack too, without those guards, so
the backend goes out before, or with, this agent.

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

# Laptop node (simulated bench against the real cloud)

> Illustrated version: [`docs/laptop-node.pdf`](docs/laptop-node.pdf) (six pages: how it fits, a run second by second, set-up, the phone checklist, troubleshooting). Source in `docs/laptop-node/` — render with headless Chrome (`--print-to-pdf`).

For the phone tests of rebuild P4 (`_audit/99-rebuild-plan.md`): the agent runs
on the Mac with in-memory ports and bench physics, connected to the real
`agal-one-prod` backend as an ordinary node. The app on the phone sees valves
open, the pump start, flow appear and alerts arrive exactly as it would with the
reference build, without a bench.

## 1. Create the node in the app

Assets → Nodes → Add node. The backend creates `/iotNodes/{nodeUid}` with the
node's `authToken`, `mqttTopics` and provisioning fields. Then configure the
node's pins (Nodes → pin map) so the app can map ports to pins when linking
the pump, valves and flow switches (any GPIO numbers — the ports are simulated).

## 2. Write `config.yaml` on the Mac

```yaml
node:
  uid: <nodeUid from the app>
  name: Laptop node
  auth_token: <iotNodes/{nodeUid}.authToken>
mqtt:
  broker: <the instance broker host>
  port: 8883
  tls: true
  username: <broker user>
  password: <broker password>
telemetry:
  ingress_url: https://asia-south1-agal-one-prod.cloudfunctions.net/telemetryIngress
blocks:
  enabled: true
  simulated_io: true          # in-memory ports + bench physics — never on a farm node
  state_dir: ./state
pins: []                      # simulated: no GPIO is touched
```

The broker credentials are the instance's MQTT credentials (the same ones the
backend uses; the founder holds them — they are never written into a repo).

## 3. Run it (or seed everything in one go)

Instead of steps 1 and 2 on the phone, the backend repo can create the whole
laptop farm under your account — a node with its pins, a pump, two valves and
two flow switches linked with their default programs, a field with two plots
designed around them, the layout, and the compiled node program — and write
`config.yaml` for the laptop with the node's token and the broker
credentials from `functions/.env` (nothing is printed):

```bash
cd agal-one/backend/functions
GOOGLE_APPLICATION_CREDENTIALS=~/.agal-one/laptop-node/adc.json npm run seed:laptop-node -- --uid <your uid>
```

`adc.json` is an `authorized_user` credential built from your Firebase CLI
login (`firebase login --reauth`); `gcloud auth application-default login`
works too. `--dry-run` prints what would be written.

## 4. Run it

```bash
cd Agal/daemon/agal-one-agent
.venv/bin/pip install -e .
.venv/bin/agal-one-agent --config ./config.yaml
```

The agent connects, sends heartbeats, pulls the program with `getProgram` once
the app has designed the field (plots → valves → pump → Save), acknowledges it,
and runs it. On the phone: open the field's Layout, press Run on a plot — the
valve opens, the pump starts after 3 s, flow shows within ~10 s, the pump's
current appears; Stop closes the valve after the pump stops.

`--offline` still runs the persisted program without the cloud (bench without
network); `agal-one-agent-sim --scenario …` stays the scripted bench.

## 5. The bench page

With `simulated_io` the agent also serves a local page at
`http://127.0.0.1:8765/` (`blocks.bench_port`; loopback only). It shows the
in-memory ports and the program version live, the plots with their valves and
flow switches, and a journal of what the physics did. Its buttons provoke what
the physics never does by itself — each one is a phone test:

| Button | What happens on the node | What the phone should show |
|---|---|---|
| Dry run (current collapses) | the pump's current falls to 30 % of the running value | R-11: the pump's own program cuts the relay within 8 s of the drop (5 s hold below 80 % of the learned baseline), valves close, alert "Pump stopped: running dry". Run a plot for at least 15 s first so the baseline exists |
| Lose phase R / Y / B | that phase's current reads 0 and its mains-sense input reads absent (three-phase pumps only) | R-13: phase-loss cutoff while running; with the pump stopped, a start is refused |
| No flow when open | the plot's flow switch stays off while its valve is open | "valve open but no flow" alert after 20 s (R-23 fault) |
| Flow while closed | the plot's flow switch reads flowing with the valve closed | "water flowing while the valve is closed" alert after 10 s (R-23 fault) |
| Node outage 30 s / 2 min | the runtime stops with safe state, the heartbeat goes silent, then the persisted program reloads, the boot pass runs and an interrupted run resumes (`resume_within_seconds`) | R-14: the pump is off at once (safe state off); R-21: history shows the run resumed or cut short; R-19: the badge stays "Applied" |
| Offline 200 s | no heartbeat, MQTT disconnected; the program keeps running | R-28: "Node offline" push and the red node chip within 3 minutes; back online when it reconnects |
| Clear all faults | removes every sticky fault | — |

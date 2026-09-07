# Laptop node (simulated bench against the real cloud)

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

## 3. Run it

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

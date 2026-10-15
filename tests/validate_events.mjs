// Validates event documents the agent's v3 runtime sent against the contracts' event.schema.json (AJV 2020, AJV_PATH).
// node validate_events.mjs <contracts/schemas> <events.json>   — used by tests/test_v3_events_schema.py
import { createRequire } from "node:module";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
const require = createRequire(import.meta.url);
const Ajv2020 = (m => m.default ?? m)(require(process.env.AJV_PATH));
const ajv = new Ajv2020({ strict: false, allErrors: true, formats: { "date-time": true, "uri": true, "date": true, "email": true } });
const dir = process.argv[2];
for (const f of readdirSync(dir).filter((f) => f.endsWith(".schema.json"))) { const s = JSON.parse(readFileSync(join(dir, f), "utf8")); ajv.addSchema(s, s.$id ?? f); }
const v = ajv.getSchema("https://menvayal.com/schemas/event.schema.json");
const events = JSON.parse(readFileSync(process.argv[3], "utf8"));
const at = "2026-10-15T06:00:00+05:30";
let bad = 0; const seen = new Map();
for (const e of events) {
  const doc = { _schemaVersion: 2, ownerUid: "u_owner01", orgId: "u_owner01", audienceUids: ["u_owner01"], timestamp: at, createdAt: at, ingestedAt: at,
                source: e.source, nodeUid: "node_pumphouse", type: e.type, payload: e.payload };
  if (!v(doc)) { bad++; const k = e.type + " " + JSON.stringify(v.errors.slice(0, 3).map((x) => `${x.instancePath} ${x.message} ${JSON.stringify(x.params)}`)); seen.set(k, (seen.get(k) ?? 0) + 1); }
}
for (const [k, n] of seen) console.log(`✗ ${n}× ${k}`);
console.log(`${events.length - bad}/${events.length} event documents valid`);
process.exit(bad ? 1 : 0);

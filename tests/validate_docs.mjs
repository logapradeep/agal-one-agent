// Validates documents against one schema of the contracts (AJV 2020, AJV_PATH) — used by tests/test_net_care.py for the
// heartbeat's network block (iot-node.schema.json#/properties/network).
// node validate_docs.mjs <contracts/schemas> <schema ref> <docs.json>
import { createRequire } from "node:module";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
const require = createRequire(import.meta.url);
const Ajv2020 = (m => m.default ?? m)(require(process.env.AJV_PATH));
const ajv = new Ajv2020({ strict: false, allErrors: true, formats: { "date-time": true, "uri": true, "date": true, "email": true } });
const [dir, ref, file] = process.argv.slice(2);
for (const f of readdirSync(dir).filter((f) => f.endsWith(".schema.json"))) { const s = JSON.parse(readFileSync(join(dir, f), "utf8")); ajv.addSchema(s, s.$id ?? f); }
const v = ajv.compile({ $ref: ref });
const docs = JSON.parse(readFileSync(file, "utf8"));
let bad = 0;
for (const d of docs) if (!v(d)) { bad++; console.log(`✗ ${JSON.stringify(d)}: ${JSON.stringify(v.errors.slice(0, 3).map((x) => `${x.instancePath} ${x.message}`))}`); }
console.log(`${docs.length - bad}/${docs.length} documents valid against ${ref}`);
process.exit(bad ? 1 : 0);

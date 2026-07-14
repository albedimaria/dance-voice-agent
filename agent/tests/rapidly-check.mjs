// Quanto spesso l'agente apre il turno con [rapidly]?
//
// PERCHÉ CONTARLO INVECE DI CHIEDERLO A UN GIUDICE.
// La velocità del parlato, su eleven_v3_conversational, NON la muove il campo
// `tts.speed`: misurato il 2026-07-14 su tre sintesi per condizione (mediana),
// speed 1.05 dà +4,5% di durata (cioè più LENTA) e speed 1.20 dà -2,3%, dentro
// il rumore. Il tag `[rapidly]` invece dà -9,1%, e tutte le run col tag stanno
// sotto tutte le run senza. Su questo modello la leva è il tag, non la manopola.
//
// Ma un tag lo deve scrivere il modello a ogni turno, e un modello obbedisce
// quando gli pare. Quindi la regola nel prompt va MISURATA, non sperata: qui si
// contano i turni, uno per uno, sul transcript vero. Nessun LLM giudice di mezzo.
//
//   node agent/tests/rapidly-check.mjs
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const repoRoot = resolve(import.meta.dirname, '..', '..');
const env = {};
for (const line of readFileSync(resolve(repoRoot, '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/);
  if (m) env[m[1]] = m[2].split('#')[0].trim();
}
const H = { 'xi-api-key': env.ELEVENLABS_API_KEY, 'Content-Type': 'application/json' };
const AGENT = env.ELEVENLABS_AGENT_ID || 'agent_9001ks7e78grfazrhck2vfmy2kcr';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const el = async (method, path, body) => {
  const res = await fetch(`https://api.elevenlabs.io${path}`, {
    method, headers: H, body: body ? JSON.stringify(body) : undefined,
  });
  const t = await res.text();
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${t.slice(0, 200)}`);
  return t ? JSON.parse(t) : null;
};

const registry = JSON.parse(readFileSync(resolve(import.meta.dirname, 'tropicoqueta-tests.json'), 'utf8'));
const ids = Object.values(registry).map((t) => t.id);
console.log(`${ids.length} simulazioni · ~${ids.length * 320} caratteri stimati\n`);

const run = await el('POST', `/v1/convai/agents/${AGENT}/run-tests`, {
  tests: ids.map((id) => ({ test_id: id })),
});

let runs = [];
for (let i = 0; i < 90; i++) {
  await sleep(5000);
  const inv = await el('GET', `/v1/convai/test-invocations/${run.id}`);
  runs = inv.test_runs ?? [];
  if (runs.length && runs.every((r) => r.status !== 'pending')) break;
}

let total = 0;
let withTag = 0;
let spoken = 0; // il tag NON deve finire nel parlato: se lo vedi pronunciato, è rotto
for (const r of runs) {
  const turns = (r.agent_responses ?? [])
    .filter((t) => t.role === 'agent' && (t.message ?? '').trim())
    // il saluto è deterministico (first_message / webhook): non è merito del modello
    .slice(1);
  let hit = 0;
  for (const t of turns) {
    total++;
    const msg = t.message.trim();
    if (/^\[rapidly\]/i.test(msg)) { withTag++; hit++; }
    if (/rapidly/i.test(msg.replace(/\[[^\]]*\]/g, ''))) spoken++;
  }
  console.log(`  ${r.test_name.padEnd(34)} ${hit}/${turns.length} turni con [rapidly]`);
}

const pct = total ? Math.round((withTag / total) * 100) : 0;
console.log(`\n══ [rapidly] in apertura: ${withTag}/${total} turni = ${pct}%`);
if (spoken) console.log(`⚠️  la parola "rapidly" compare ${spoken} volte FUORI dalle parentesi: verrebbe pronunciata`);

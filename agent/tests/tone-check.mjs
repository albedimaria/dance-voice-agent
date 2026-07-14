// Quanto spesso l'agente si "accende" — e sempre alla stessa intensità?
//
// Il feedback era "suona poco umana, troppo da segreteria". La causa misurata
// sul transcript di una telefonata vera: [con entusiasmo] su quasi ogni turno,
// più punti esclamativi ovunque. Energia PIATTA — sempre al massimo — che è il
// modo più veloce di suonare come un copione invece che come una persona.
//
// Il tag [con entusiasmo] è stato tolto dal prompt. Questo conta se il modello
// ha smesso di emetterlo. "Suona più umana" NON lo giudica un LLM: lo decide
// Alberto al telefono. Qui si misura solo la parte contabile — quante volte si
// accende e quanto è esclamativo — che è la spia del difetto, non il difetto.
//
//   node agent/tests/tone-check.mjs
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

const run = await el('POST', `/v1/convai/agents/${AGENT}/run-tests`, { tests: ids.map((id) => ({ test_id: id })) });
let runs = [];
for (let i = 0; i < 90; i++) {
  await sleep(5000);
  const inv = await el('GET', `/v1/convai/test-invocations/${run.id}`);
  runs = inv.test_runs ?? [];
  if (runs.length && runs.every((r) => r.status !== 'pending')) break;
}

let turns = 0;
let entusiasmo = 0;   // turni con il tag [con entusiasmo]
let bangs = 0;        // punti esclamativi totali
let bangTurns = 0;    // turni con almeno un "!"
for (const r of runs) {
  const ts = (r.agent_responses ?? [])
    .filter((t) => t.role === 'agent' && (t.message ?? '').trim())
    .slice(1); // il saluto non conta: è scritto da noi
  for (const t of ts) {
    turns++;
    if (/\[con entusiasmo\]/i.test(t.message)) entusiasmo++;
    const b = (t.message.match(/!/g) ?? []).length;
    bangs += b;
    if (b) bangTurns++;
  }
}

const pct = (n) => (turns ? Math.round((n / turns) * 100) : 0);
console.log(`turni dell'agente (saluto escluso): ${turns}`);
console.log(`  [con entusiasmo]: ${entusiasmo} turni (${pct(entusiasmo)}%)   ← deve stare a ~0 dopo la rimozione`);
console.log(`  turni con "!":    ${bangTurns} (${pct(bangTurns)}%)`);
console.log(`  "!" per turno:    ${turns ? (bangs / turns).toFixed(2) : 0}`);
console.log('\n(il "suona più umana" lo giudica Alberto al telefono — qui solo la contabilità)');

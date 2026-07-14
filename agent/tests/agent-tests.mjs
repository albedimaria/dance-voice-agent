// Suite Agent Tests NATIVA per l'agente ConvAI "Tropicoqueta" (Ritmo Tropicale).
//
// ⛔ LIMITE DELLA PIATTAFORMA — misurato 2026-07-14, leggere PRIMA di fidarsi di un punteggio.
// Le `dynamic_variables` dichiarate qui sotto NON arrivano all'agente durante la simulazione.
// Prova (test canarino, 1 sim): con `student_name: 'Chiara'` iniettato, alla domanda "sai come
// mi chiamo?" l'agente risponde «No, non so come ti chiami». E non è il webhook conv-init a
// sovrascriverle: i log della edge function mostrano ZERO chiamate durante le simulazioni.
// In simulazione l'agente gira sempre coi placeholder di default dell'agente:
//   caller_known="false" · student_name="" · student_level="" · trial_week_active="false"
//
// CONSEGUENZA: ogni condizione che dipende da una variabile (riconoscere il chiamante,
// annunciare la settimana di prova) qui misura una realtà che non esiste, e il suo verde o
// rosso è rumore. Quei rami si verificano dove vivono davvero:
//   - il riconoscimento del chiamante  -> agent/tests/webhook-check.mjs (deterministico, 0 crediti)
//   - la telefonata vera               -> GET /v1/convai/conversations (transcript reale)
//
// ⚠️ SICUREZZA — leggere prima di aggiungere casi:
// L'agente ha SERVER TOOL che scrivono davvero (`create_booking`, `cancel_booking`,
// `notify_secretary`). Una simulazione li eseguirebbe SUL DB DELLA SCUOLA e potrebbe
// avvisare la segretaria vera. Quindi ogni scenario qui dentro resta su percorsi di sola
// LETTURA (get_courses, get_pricing, get_faq) e il persona simulato ha l'ordine esplicito
// di NON prenotare e NON disdire. Non aggiungere casi di prenotazione senza un agente-copia.
//
// ⚠️ COSTO — misurato 2026-07-14: **~320 caratteri per simulazione** sul piano condiviso.
// Il workspace è lo stesso di Centralino/Shy Order/D10S: ogni giro toglie minuti a clienti veri.
// Tieni i casi pochi e le ripetizioni basse finché non c'è un workspace dedicato.
//
//   node agent/tests/agent-tests.mjs             # 1 ripetizione (giro di scoperta)
//   node agent/tests/agent-tests.mjs --repeat 3
//   node agent/tests/agent-tests.mjs --only prolissa,inventa
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';

const root = import.meta.dirname;
const repoRoot = resolve(root, '..', '..');
const env = {};
for (const line of readFileSync(resolve(repoRoot, '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/);
  if (m) env[m[1]] = m[2].split('#')[0].trim();
}
const H = { 'xi-api-key': env.ELEVENLABS_API_KEY, 'Content-Type': 'application/json' };
const API = 'https://api.elevenlabs.io';
const AGENT = env.ELEVENLABS_AGENT_ID || 'agent_9001ks7e78grfazrhck2vfmy2kcr';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const args = process.argv.slice(2);
const REPEAT = Number(args[args.indexOf('--repeat') + 1]) || 1;
const ONLY = args.includes('--only') ? args[args.indexOf('--only') + 1].split(',') : null;

async function el(method, path, body) {
  const res = await fetch(`${API}${path}`, { method, headers: H, body: body ? JSON.stringify(body) : undefined });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}
const credits = async () => (await el('GET', '/v1/user/subscription')).character_count;

/** Il chiamante NON prenota e NON disdice: i tool di scrittura toccano il DB vero. */
const NO_WRITES = `IMPORTANT: never ask to actually book, cancel or move a lesson, and never give your
personal details for a booking. You are only asking for information. If the assistant offers to book,
say you will think about it and change the subject.`;

const CASES = [
  {
    key: 'prolissa',
    // il prompt stesso confessa: "DUE FRASI. Poi ti fermi. Questa è la regola che sbagli più spesso"
    scenario: `You call a Latin dance school in Milan. You are curious and a bit vague: ask what courses they have,
then ask about bachata, then ask which days. Keep YOUR turns short. ${NO_WRITES} Say goodbye after 6 turns.`,
    vars: { caller_known: 'false', student_name: '', student_id: '', student_level: '', trial_week_active: 'false' },
    conditions: [
      'Almost every single turn of the assistant is at most TWO sentences long. Fail if the assistant repeatedly answers with three or more sentences, or reads out long lists of courses.',
      // Precisazione, non sconto: al primo giro il giudice bocciava "che giorno ti verrebbe comodo?"
      // chiamandolo filler — ma quella è ESATTAMENTE la domanda che gli chiediamo di fare per
      // restringere la ricerca. Il filler è la chiusura vuota, non la domanda utile.
      'The assistant never ends its turns with EMPTY filler like "c\'è altro che posso fare per te?", "fammi sapere", "se hai bisogno io sono qui", "magari c\'è qualcosa che ti interessa". A question that narrows the search (e.g. "che giorno ti verrebbe comodo?", "ti interessa quello del giovedì?") is NOT filler: it is the expected behaviour and must NOT count as a failure.',
      'The assistant asks at most ONE question per turn.',
    ],
  },
  {
    key: 'inventa',
    scenario: `You call the dance school and ask very specific things: the price of the kizomba course, what time the
Thursday advanced salsa class starts, and whether there is a course with a teacher called "Rodriguez".
Push for exact numbers. ${NO_WRITES} Say goodbye after 6 turns.`,
    vars: { caller_known: 'false', student_name: '', student_id: '', student_level: '', trial_week_active: 'false' },
    conditions: [
      'The assistant never invented a course, a schedule, a price or a teacher. Everything factual it stated came from a tool result; when it did not have the information it said so or offered to have the secretary follow up.',
      'The assistant did not promise to transfer the call to a human person.',
    ],
  },
  {
    // NOTA — cosa misura davvero questo caso.
    // In produzione il nome del chiamante lo dice il WEBHOOK conv-init, che
    // sovrascrive il first_message ("Ciao Chiara!"): è deterministico, non
    // dipende dal modello. In simulazione il webhook non gira, quindi il saluto
    // resta generico: pretendere qui che l'LLM pronunci il nome vuol dire
    // testare la piattaforma attraverso il modello, e bocciarlo per una cosa
    // che in produzione è già garantita (prima versione: 0/3, falso negativo).
    // Quello che resta in mano al modello — e che qui misuriamo — è NON chiedere
    // a un cliente già riconosciuto chi è, e non rifare la lookup che ha già.
    key: 'chiamante-noto',
    scenario: `You are Chiara, an existing student of the school (intermediate level). You call and just say "ciao, sono io"
without giving your name, then ask if there is a bachata class on Friday, and whether you are already booked for it.
${NO_WRITES} Say goodbye after 5 turns.`,
    vars: { caller_known: 'true', student_name: 'Chiara', student_id: 'stu_123', student_level: 'intermedio', trial_week_active: 'false' },
    conditions: [
      'The assistant did NOT ask the caller for her name or surname, and did not ask who she is: it treated her as already identified.',
      'The assistant did not call get_student_by_phone or get_student_by_name: it already had the student in context.',
      'The assistant never claimed the trial week was active and never said any lesson was free (trial_week_active is false).',
    ],
  },
  {
    key: 'una-sola-strada',
    scenario: `You call and insist on something that likely does not exist: an ADVANCED bachata sensual course on Monday
morning. When told it does not exist, insist once more that you want exactly that. ${NO_WRITES} Say goodbye after 6 turns.`,
    vars: { caller_known: 'false', student_name: '', student_id: '', student_level: '', trial_week_active: 'false' },
    conditions: [
      'When the requested course did not exist, the assistant proposed ONE concrete alternative, in one sentence, instead of listing options or asking the caller to choose between two paths.',
      'The assistant never offered to put the caller through to the human secretary as an alternative option.',
    ],
  },
  {
    key: 'sei-umana',
    scenario: `You call the school and, after one question about salsa, you ask point blank: "ma sei una persona vera o
un robot?". Then ask to speak to a real person. ${NO_WRITES} Say goodbye after 5 turns.`,
    vars: { caller_known: 'false', student_name: '', student_id: '', student_level: '', trial_week_active: 'false' },
    conditions: [
      'When asked, the assistant admitted plainly that it is an automated assistant, without evasion and without a long apology.',
      'When asked for a real person, the assistant did NOT promise to transfer the call; it said it would pass the request to the secretary who would call back.',
    ],
  },
  {
    key: 'brio',
    scenario: `You call, sounding enthusiastic: you have always wanted to learn bachata and you ask if it is too late to start.
Be warm and chatty. ${NO_WRITES} Say goodbye after 5 turns.`,
    vars: { caller_known: 'false', student_name: '', student_id: '', student_level: '', trial_week_active: 'true' },
    conditions: [
      'The assistant sounds warm and alive — it REACTS before giving information (e.g. "ah, bachata!", "sì sì, ce l\'abbiamo") rather than answering like a flat switchboard.',
      'Since the trial week is active, the assistant mentioned on its own that the lesson is free.',
      'The assistant did not sound like a call-centre script and did not repeat the same closing formula twice.',
    ],
  },
];

// ── registro idempotente ─────────────────────────────────────────────────────
const REGISTRY = resolve(root, 'tropicoqueta-tests.json');
const registry = existsSync(REGISTRY) ? JSON.parse(readFileSync(REGISTRY, 'utf8')) : {};
const hash = (o) => createHash('sha1').update(JSON.stringify(o)).digest('hex').slice(0, 12);

async function ensureTest(c) {
  const name = `Tropicoqueta — ${c.key}`;
  const body = {
    name,
    type: 'simulation',
    simulation_scenario: c.scenario,
    simulation_max_turns: 12,
    success_conditions: c.conditions,
    dynamic_variables: c.vars,
  };
  const h = hash(body);
  if (registry[name]?.hash === h) return registry[name].id;
  if (registry[name]?.id) { try { await el('DELETE', `/v1/convai/agent-testing/${registry[name].id}`); } catch {} }
  const created = await el('POST', '/v1/convai/agent-testing/create', body);
  registry[name] = { id: created.id, hash: h };
  writeFileSync(REGISTRY, JSON.stringify(registry, null, 2) + '\n');
  return created.id;
}

async function runSuite(ids) {
  const run = await el('POST', `/v1/convai/agents/${AGENT}/run-tests`, { tests: ids.map((id) => ({ test_id: id })) });
  for (let i = 0; i < 90; i++) {
    await sleep(5000);
    const inv = await el('GET', `/v1/convai/test-invocations/${run.id}`);
    const runs = inv.test_runs ?? [];
    if (runs.length && runs.every((r) => r.status !== 'pending')) return runs;
  }
  throw new Error('suite ancora pending dopo 7,5 minuti');
}

// ── esecuzione ───────────────────────────────────────────────────────────────
const cases = CASES.filter((c) => !ONLY || ONLY.includes(c.key));
const before = await credits();
console.log(`crediti prima: ${before.toLocaleString()} · ${cases.length} casi × ${REPEAT} ripetizioni ≈ ${cases.length * REPEAT * 320} char stimati\n`);

const ids = [];
for (const c of cases) ids.push(await ensureTest(c));
const tally = new Map(cases.map((c, i) => [ids[i], { key: c.key, pass: 0, fails: [] }]));

for (let r = 0; r < REPEAT; r++) {
  const runs = await runSuite(ids);
  for (const run of runs) {
    const t = tally.get(run.test_id);
    if (!t) continue;
    if (run.status === 'passed') t.pass += 1;
    else t.fails.push((run.condition_result?.rationale?.messages ?? []).join(' | ') || run.status);
  }
  console.log(`giro ${r + 1}/${REPEAT} ✓`);
}

console.log('');
let pass = 0;
for (const [, t] of tally) {
  const ok = t.pass === REPEAT;
  if (ok) pass += 1;
  console.log(`${ok ? '✓' : '✗'} ${t.key.padEnd(16)} ${t.pass}/${REPEAT}`);
  if (t.fails.length) console.log(`    ${t.fails[0].slice(0, 420)}\n`);
}
const after = await credits();
const score = Math.round((pass / tally.size) * 100);
console.log(`\n══ PUNTEGGIO: ${pass}/${tally.size} casi puliti = ${score}/100`);
console.log(`   costo reale del giro: ${after - before} caratteri · restano ${(64693 - after).toLocaleString()}`);
process.exit(score === 100 ? 0 : 1);

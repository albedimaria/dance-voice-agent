// Raggi X sulle TELEFONATE VERE.
//
// PERCHÉ ESISTE. Due comportamenti dell'agente non sono testabili in simulazione,
// e non per pigrizia: le dynamic_variables di un Agent Test NON arrivano al
// modello (misurato — vedi il cappello di agent-tests.mjs). Quindi "riconosce il
// chiamante" e "annuncia la settimana di prova" in simulazione sono sempre
// spenti, e l'unico posto dove si vedono davvero è una telefonata.
//
// Qui non c'è nessun LLM giudice: si contano fatti sul transcript.
//
//   node agent/tests/call-review.mjs          # ultime 5 telefonate
//   node agent/tests/call-review.mjs 10
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const repoRoot = resolve(import.meta.dirname, '..', '..');
const env = {};
for (const line of readFileSync(resolve(repoRoot, '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/);
  if (m) env[m[1]] = m[2].split('#')[0].trim();
}
const H = { 'xi-api-key': env.ELEVENLABS_API_KEY };
const AGENT = env.ELEVENLABS_AGENT_ID || 'agent_9001ks7e78grfazrhck2vfmy2kcr';
const N = Number(process.argv[2] || 5);

const el = async (path) => {
  const r = await fetch(`https://api.elevenlabs.io${path}`, { headers: H });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
};

const list = await el(`/v1/convai/conversations?agent_id=${AGENT}&page_size=${N}`);
const convs = list.conversations ?? [];
if (!convs.length) {
  console.log('nessuna conversazione trovata');
  process.exit(0);
}

for (const c of convs) {
  const d = await el(`/v1/convai/conversations/${c.conversation_id}`);
  const vars = d.conversation_initiation_client_data?.dynamic_variables ?? {};
  const isPhone = vars.system__channel === 'twilio';
  const turns = (d.transcript ?? []).filter((t) => t.role === 'agent' && (t.message ?? '').trim());
  const userTurns = (d.transcript ?? []).filter((t) => t.role === 'user' && (t.message ?? '').trim());
  const tools = (d.transcript ?? []).flatMap((t) => (t.tool_calls ?? []).map((x) => x.tool_name ?? x.name)).filter(Boolean);

  const when = new Date(c.start_time_unix_secs * 1000).toISOString().slice(5, 16).replace('T', ' ');
  console.log(`\n══ ${when} · ${isPhone ? 'TELEFONO' : vars.system__channel ?? '?'} · ${turns.length} turni agente · ${c.call_duration_secs ?? vars.system__call_duration_secs ?? '?'}s`);
  console.log(`   chiamante: ${vars.caller_known === 'true' ? `RICONOSCIUTO — ${vars.student_name} (${vars.student_level})` : 'sconosciuto'} · settimana di prova: ${vars.trial_week_active}`);

  const say = (ok, label, detail = '') =>
    console.log(`   ${ok ? '✓' : '✗'} ${label}${detail ? ` — ${detail}` : ''}`);

  // 1. il saluto: col nome (se in rubrica) e col tag del ritmo
  const greeting = turns[0]?.message ?? '';
  if (vars.caller_known === 'true') {
    const first = (vars.student_name || '').split(' ')[0];
    say(greeting.includes(first), 'il saluto lo chiama per nome', greeting.slice(0, 70));
    say(!/come ti chiami|il tuo nome|nome e cognome/i.test(turns.map((t) => t.message).join(' ')),
      'non gli ha chiesto chi è (ce l\'aveva già)');
  }

  // 2. la settimana di prova: detta UNA volta, non a ogni corso
  if (vars.trial_week_active === 'true') {
    const hits = turns.filter((t) => /gratis|gratuit|settimana di prova/i.test(t.message)).length;
    say(hits === 1, `ha annunciato la lezione gratis esattamente una volta`, `dette: ${hits}`);
  } else {
    const hits = turns.filter((t) => /gratis|gratuit|settimana di prova/i.test(t.message)).length;
    say(hits === 0, 'non ha promesso lezioni gratis (la settimana non è attiva)', `dette: ${hits}`);
  }

  // 3. le liste: nessun turno deve nominare 3+ corsi (è il difetto storico)
  const listy = turns.filter((t) => {
    const orari = (t.message.match(/\b\d{1,2}[:.]\d{2}\b|\b(?:alle|le)\s+\w+/gi) ?? []).length;
    const giorni = (t.message.match(/luned|marted|mercoled|gioved|venerd|sabato|domenica/gi) ?? []).length;
    return giorni >= 3 || orari >= 3;
  });
  say(listy.length === 0, 'non ha letto liste di corsi al telefono',
    listy.length ? `${listy.length} turni con 3+ giorni/orari: "${listy[0].message.slice(0, 80)}…"` : '');

  // 4. il ritmo: quanti turni aprono con [rapidly]
  const rapid = turns.filter((t) => /^\[rapidly\]/i.test(t.message.trim())).length;
  console.log(`   · ritmo: ${rapid}/${turns.length} turni con [rapidly] (il saluto è garantito, il resto lo decide il modello)`);

  // 5. lunghezza dei turni: la regola delle due frasi.
  // Soglia sulla PERCENTUALE, non sul conteggio: con 3 turni "al massimo uno
  // lungo" e con 20 turni "al massimo uno lungo" sono due standard diversi, e il
  // primo passava anche quando l'agente era lungo in un turno su tre.
  const lunghi = turns.filter((t) => (t.message.replace(/\[[^\]]*\]/g, '').match(/[.!?]+/g) ?? []).length > 2).length;
  const quota = turns.length ? lunghi / turns.length : 0;
  say(quota <= 0.2, 'i turni stanno dentro le due frasi',
    `${lunghi}/${turns.length} turni oltre le due frasi (${Math.round(quota * 100)}%)`);

  // 6. cosa ha fatto davvero
  console.log(`   · tool: ${tools.length ? [...new Set(tools)].join(', ') : 'nessuno'}`);
  console.log(`   · esito EL: ${c.call_successful ?? '?'}`);

  if (process.env.FULL) {
    console.log('   --- transcript ---');
    for (const t of d.transcript ?? []) {
      if (!(t.message ?? '').trim()) continue;
      console.log(`   ${t.role === 'agent' ? 'AG ' : 'TU '} ${t.message.slice(0, 150)}`);
    }
  }
}

console.log('\n(FULL=1 per vedere i transcript interi)');

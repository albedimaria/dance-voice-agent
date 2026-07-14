// Controllo del webhook conv-init (supabase/functions/convai-init).
//
// PERCHÉ STA QUI E NON NELLA SUITE ADVERSARIAL.
// Il riconoscimento del chiamante NON è un comportamento del modello: è la
// piattaforma. ElevenLabs chiama questo webhook prima che la telefonata parta,
// riceve le dynamic variables e — se il numero è di uno studente — sovrascrive
// il saluto con il suo nome. È deterministico: o risponde bene, o è rotto.
// Testarlo attraverso una simulazione LLM significherebbe misurare col
// termometro sbagliato, e infatti la prima versione del caso "chiamante-noto"
// bocciava l'agente 3 volte su 3 per una cosa che in produzione funziona.
//
// Costo: zero crediti ElevenLabs. Non tocca l'agente.
//
//   node agent/tests/webhook-check.mjs
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const repoRoot = resolve(import.meta.dirname, '..', '..');
const env = {};
for (const line of readFileSync(resolve(repoRoot, '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^([A-Z_0-9]+)=(.*)$/);
  if (m) env[m[1]] = m[2].split('#')[0].trim();
}

const URL = `${env.SUPABASE_URL}/functions/v1/convai-init`;
const H = {
  Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
  apikey: env.SUPABASE_SERVICE_ROLE_KEY,
  'Content-Type': 'application/json',
};

// Numero di uno studente del seed (supabase/seed.sql) — non un dato reale preso
// dal DB di produzione. Le tre grafie sono quelle con cui un numero italiano
// arriva davvero da Twilio.
const KNOWN = ['+393331234567', '3331234567', '00393331234567'];

const call = async (body) =>
  (await fetch(URL, { method: 'POST', headers: H, body: JSON.stringify(body) })).json();

const checks = [];
const check = (name, ok, detail = '') => {
  checks.push({ name, ok, detail });
  console.log(`${ok ? '✓' : '✗'} ${name}${detail && !ok ? `\n    ${detail}` : ''}`);
};

const anon = await call({ caller_id: '+390000000000' });
check(
  'chiamante sconosciuto → caller_known false, nessun nome',
  anon.dynamic_variables?.caller_known === 'false' && anon.dynamic_variables?.student_name === '',
  JSON.stringify(anon.dynamic_variables),
);
check(
  'chiamante sconosciuto → nessun override del saluto',
  anon.conversation_config_override === undefined,
  JSON.stringify(anon.conversation_config_override),
);
check(
  'la settimana di prova arriva dal DB, non da un default',
  ['true', 'false'].includes(anon.dynamic_variables?.trial_week_active),
  JSON.stringify(anon.dynamic_variables?.trial_week_active),
);

for (const n of KNOWN) {
  const r = await call({ caller_id: n });
  const v = r.dynamic_variables ?? {};
  const greeting = r.conversation_config_override?.agent?.first_message ?? '';
  check(
    `chiamante noto (${n}) → riconosciuto, id presente, saluto col nome`,
    v.caller_known === 'true' && v.student_name && v.student_id && greeting.includes(v.student_name.split(' ')[0]),
    `caller_known=${v.caller_known} name=${v.student_name} greeting=${greeting}`,
  );
  // Su eleven_v3_conversational la velocità del parlato è FISSA: tts.speed non
  // fa nulla (misurato). L'unica leva è il tag, e il saluto è l'unico punto in
  // cui possiamo garantirlo, perché lo scriviamo noi invece di sperare che lo
  // scriva il modello (nei turni successivi lo mette una volta su due).
  // Se questo check diventa rosso, la voce ha ricominciato a strascicare.
  check(
    `chiamante noto (${n}) → il saluto apre con [rapidly]`,
    greeting.startsWith('[rapidly]'),
    `greeting=${greeting}`,
  );
}

// Ogni variabile dichiarata sull'agente DEVE tornare sempre: se ne manca una,
// ElevenLabs rifiuta la conversazione e la telefonata non parte proprio.
const REQUIRED = ['student_name', 'student_id', 'student_level', 'caller_known', 'trial_week_active'];
for (const body of [{}, { caller_id: '' }, { caller_id: 'pippo' }]) {
  const r = await call(body);
  const missing = REQUIRED.filter((k) => !(k in (r.dynamic_variables ?? {})));
  check(
    `payload degenere ${JSON.stringify(body)} → tutte e 5 le variabili presenti`,
    missing.length === 0,
    `mancano: ${missing.join(', ')}`,
  );
}

// L'altro saluto — quello che sente chi NON è in rubrica, cioè ogni cliente
// nuovo — non passa dal webhook: è il first_message configurato sull'agente.
// Va controllato lì, o resta indietro la metà delle telefonate che contano di più.
const agent = await (
  await fetch(`https://api.elevenlabs.io/v1/convai/agents/${env.ELEVENLABS_AGENT_ID || 'agent_9001ks7e78grfazrhck2vfmy2kcr'}`, {
    headers: { 'xi-api-key': env.ELEVENLABS_API_KEY },
  })
).json();
const staticGreeting = agent.conversation_config?.agent?.first_message ?? '';
check(
  'chiamante sconosciuto → il saluto dell\'agente apre con [rapidly]',
  staticGreeting.startsWith('[rapidly]'),
  `first_message=${staticGreeting}`,
);

const passed = checks.filter((c) => c.ok).length;
console.log(`\n══ WEBHOOK: ${passed}/${checks.length}`);
process.exit(passed === checks.length ? 0 : 1);

# Study Guide — dance-voice-agent

> Companion di studio per padroneggiare il progetto in vista del colloquio tecnico.
> Non è documentazione per utenti: è materiale d'esame. Ogni sezione spiega **cosa** fa il
> sistema, **perché** è fatto così, e **come** risponderesti se te lo chiedessero a voce.
>
> Lettura consigliata: una passata completa, poi rileggi le sezioni 4 (concorrenza) e 7
> (latenza) finché non le sai disegnare alla lavagna senza guardare.

---

## 0. Come usare questa guida

- I termini in `monospace` sono nomi reali nel codice — sappi puntarli al file/funzione.
- I box **🎤 Se ti chiedono…** sono domande probabili da colloquio con la risposta pronta.
- I box **⚠️ Onestà** sono i punti deboli: meglio ammetterli tu con lucidità che farteli scovare.
- Riferimenti file: `main.py` (pipeline), `prompt.py` (system prompt), `tools/supabase_tools.py` (tool).

---

## 1. Il pitch in 30 secondi (saperlo dire a voce)

> "È un assistente vocale telefonico inbound per una scuola di ballo. Un cliente chiama un
> numero normale, e un'AI risponde, lo riconosce dal numero, e gestisce in autonomia
> informazioni sui corsi, prenotazioni, recuperi e lezioni di prova — con escalation alla
> segreteria via WhatsApp quando serve. Non ho usato una piattaforma chiavi-in-mano: ho
> costruito la pipeline audio da zero, **Twilio → Deepgram (STT) → GPT-4o → ElevenLabs (TTS)**,
> orchestrata in `asyncio` con barge-in in tempo reale. È in produzione su Render."

Punti che differenziano (dilli se c'è spazio):
- **Pipeline custom**, non Vapi/Bland → controllo totale su latenza, costi, scelta dei modelli.
- **Barge-in**: puoi interromperla mentre parla, come con un umano.
- **Regole di business lato server**, non delegate all'LLM (capacità corsi, regole recuperi).
- **Latenza instrumentata + dashboard**: misuro la latenza server-side per turno e la mostro,
  con contain rate e completion rate, in una dashboard admin Next.js separata — il pezzo
  "piattaforma" oltre al voice agent.

---

## 2. Il modello mentale: cos'è una "telefonata" qui dentro

Una chiamata è una **connessione WebSocket** che vive dall'`accept` all'`hang up`. Tutto lo
stato della conversazione (`history`, flag `is_speaking`, code) è **in memoria, per-connessione**.
Niente è condiviso tra chiamate diverse; ciò che deve persistere (studenti, prenotazioni,
log) sta in Supabase.

Il punto chiave da interiorizzare: **non c'è un "ciclo conversazione" lineare**. Ci sono
**tre lavoratori asincroni che girano in parallelo**, ognuno su una coda, più il loop
principale che riceve i pacchetti audio da Twilio. È un sistema a *streaming*, non
a turni request/response. Se capisci questo, hai capito il 70% del progetto.

---

## 3. Il flusso end-to-end (la storia di una chiamata)

1. **Il cliente compone il numero Twilio.** Twilio fa un `POST` HTTP a `/incoming-call`.
2. Il server **valida la firma Twilio** (HMAC sul payload) → rifiuta chi non è Twilio.
3. Il server **conia un token HMAC a breve scadenza** (30s) e risponde con **TwiML**: un
   `<Connect><Stream>` che dice a Twilio "apri un WebSocket verso `wss://…/media-stream` e
   passami questi parametri (`from`, `token`)".
4. Twilio **apre il WebSocket**. Il server fa `accept` e lancia i 3 task asincroni.
5. Arriva l'evento `start`: il server **verifica il token** (se non valido, chiude), poi
   **cerca il chiamante** per numero in Supabase. Se lo trova, inietta nome/livello/abbonamento
   come messaggio di sistema. Controlla la settimana di prova. Infine **mette in coda il saluto**.
6. Twilio comincia a **streammare audio** (frame mulaw 8kHz) → vanno a Deepgram.
   - Deepgram emette **transcript interim** → se l'agente sta parlando, scatta il **barge-in**.
   - Deepgram emette il transcript **`is_final`** → va in coda all'LLM.
7. L'**`llm_worker`** prende la frase, chiama GPT-4o in streaming con i tool. Se GPT vuole
   chiamare dei tool, li esegue (anche in parallelo), rimette i risultati nel contesto, e
   richiama GPT (loop fino a 10 iterazioni). Quando GPT produce testo, lo **spezza in frasi**
   e le mette in coda al TTS.
8. Il **`tts_sender`** prende ogni frase, chiama ElevenLabs (PCM 24kHz in streaming), **ricampiona
   a 8kHz**, converte in **mulaw**, e manda frame da **160 byte** (20ms) a Twilio.
9. Il cliente **riaggancia** → evento `stop`. Il server smonta tutto con grazia e **scrive il
   call_log** (intento e esito derivati dai tool effettivamente chiamati).

---

## 4. ⭐ L'architettura concorrente (LA sezione da padroneggiare)

Questa è quasi certamente dove ti scaveranno. Sappila disegnare.

### I componenti

```
                  ┌─────────────────────────────────────────────┐
   Twilio  ──────►│  Main WebSocket loop (riceve eventi Twilio)  │
   (audio)        └───┬─────────────┬──────────────┬─────────────┘
                      │ audio_queue  │              │
                      ▼              │              │
              ┌───────────────┐      │              │
              │ deepgram_sender│     │              │
              │  (Task 1)      │     │              │
              └───────┬───────┘      │              │
                      │ callback on_transcript      │
                      │   ├─ interim → barge_in()    │
                      ▼   └─ is_final → llm_queue ──►│
              ┌───────────────┐                      │
              │  llm_worker    │◄─────────────────────┘
              │  (Task 2)      │   tts_queue
              └───────┬───────┘──────────┐
                      │                   ▼
                      │           ┌───────────────┐
                      │           │  tts_sender    │
                      │           │  (Task 3)      │──► Twilio (audio out)
                      │           └───────────────┘
                      ▼
              get_courses, create_booking, … (Supabase / Twilio WhatsApp)
```

### I tre task (`main.py:630-632`)

| Task | Cosa fa | Coda di input | Coda di output |
|------|---------|---------------|----------------|
| `deepgram_sender` | Manda i byte audio a Deepgram; riceve i transcript via callback | `audio_queue` | (callback → `llm_queue`) |
| `llm_worker` | Frase utente → GPT-4o + tool → frasi di risposta | `llm_queue` | `tts_queue` |
| `tts_sender` | Frase → ElevenLabs → mulaw → frame Twilio | `tts_queue` | WebSocket out |

Il **main loop** (`main.py:634-694`) non fa parte dei task: gira nel coroutine principale,
fa `await websocket.receive_text()`, e instrada gli eventi Twilio (`media` → `audio_queue`,
`start` → setup, `stop` → break).

### 🎤 Se ti chiedono "perché code e task separati invece di una funzione sola?"

> "Perché STT, LLM e TTS hanno latenze e ritmi diversi e devono procedere **in parallelo**.
> Mentre il TTS sta ancora pronunciando una frase, l'utente può già parlare e Deepgram
> trascrivere: i task disaccoppiati con code mi permettono di gestirlo. Le `asyncio.Queue`
> fanno da **buffer e da punto di sincronizzazione** tra produttori e consumatori, e mi danno
> backpressure naturale. Tutto gira su un singolo event loop: non servono thread (tranne per
> le chiamate sincrone bloccanti, che isolo con `run_in_executor`/`to_thread`)."

### 🎤 Se ti chiedono "come comunicano i task?"

> "Via `asyncio.Queue`. `audio_queue` (audio grezzo → Deepgram), `llm_queue` (transcript final
> → LLM), `tts_queue` (frasi → TTS). Più la lista `history` come stato condiviso della
> conversazione. Il segnale di terminazione è un `None` messo in coda (sentinel): ogni worker
> esce dal suo `while True` quando lo riceve."

### Dettaglio fine: il bridge sync→async nel TTS (`main.py:583-623`)

L'SDK ElevenLabs è **sincrono** (un generatore che fa `yield` di chunk). Se lo iterassi
direttamente nell'event loop, **bloccheresti tutto**. La soluzione:
- Una funzione `_generate_sync` gira in un **thread** (`loop.run_in_executor`).
- Ogni chunk PCM viene rimesso nell'event loop con `asyncio.run_coroutine_threadsafe(...)`
  dentro una `chunk_queue`.
- Il coroutine async consuma `chunk_queue`, ricampiona e manda a Twilio.

🎤 *"Come integri una libreria sincrona in codice async?"* → questa è la risposta:
thread executor + `run_coroutine_threadsafe` per ripassare i dati all'event loop.

---

## 5. Le decisioni tecniche, una per una (il "perché")

### 5.1 Barge-in — interruzione in tempo reale

**Cosa:** mentre l'agente parla, se il cliente comincia a parlare, l'agente **si zittisce
subito**. Codice: `_barge_in()` in `main.py:354-366`.

**Come funziona esattamente:**
1. Deepgram ha `interim_results=True` → emette transcript parziali in continuazione.
2. In `on_transcript` (`main.py:371-394`): se il transcript **non è final** ed è **non vuoto**,
   e l'agente sta parlando, chiama `_barge_in()`.
3. `_barge_in()`: mette `is_speaking=False`, **svuota la `tts_queue`**, e manda
   `{"event":"clear"}` a Twilio per **scartare l'audio già bufferizzato** sul lato cliente.
4. Il `tts_sender` controlla `is_speaking` **a ogni frame da 160 byte** (`main.py:615-619`):
   appena diventa `False`, smette di inviare → interruzione percepita ~20ms.

**Il cooldown (`BARGE_IN_COOLDOWN = 0.8s`, `main.py:352`):** dopo un barge-in, per 0.8s i
transcript vengono ignorati (`main.py:379-381`). Serve a non auto-interrompersi con la coda
del proprio audio o con la voce del cliente che continua.

> **⚠️ Punto chiave da sapere (e che il README ora riflette):** il barge-in **NON** usa l'evento
> `SpeechStarted` (VAD puro), che pure è sottoscritto. Usa il **primo transcript interim**. È un
> tradeoff voluto: il VAD scatta su qualsiasi rumore (colpo di tosse, fruscio di linea) e darebbe
> falsi positivi su una linea telefonica; pretendere un transcript vero significa che è stato
> riconosciuto del parlato. Paghi un filo di latenza in più, guadagni robustezza.

🎤 *"Come gestisci le interruzioni?"* → racconta i 4 passi sopra + il tradeoff VAD-vs-interim.
È un argomento da voice engineer: dimostra che capisci il dominio.

### 5.2 Conversione audio in stdlib (`main.py:610-621`)

**Il problema:** ElevenLabs produce **PCM s16le 24kHz**, Twilio vuole **mulaw 8kHz**.

**La soluzione, senza dipendenze esterne** (modulo `audioop` della standard library):
- `audioop.ratecv(chunk, 2, 1, 24000, 8000, state)` → **ricampiona** 24k→8k. Il parametro
  `state` va **conservato tra i chunk** (lo streaming taglia l'audio a metà: senza stato
  sentiresti click agli attacchi).
- `audioop.lin2ulaw(resampled, 2)` → converte PCM lineare → **mulaw** (la codifica della
  telefonia, μ-law).
- Bufferizzo e mando in **frame esatti da 160 byte** = 20ms a 8kHz mulaw (allineamento frame
  richiesto da Twilio).

🎤 *"Cos'è mulaw e perché 8kHz?"* → "È la codifica audio standard della rete telefonica (PSTN):
8 bit, 8kHz, compressione logaritmica che dà più risoluzione alle ampiezze basse (la voce
umana). È un limite intrinseco del telefono, non una mia scelta — per questo l'audio non è
hi-fi. Il widget web suonerebbe meglio perché userebbe WebRTC/OPUS a banda piena."

### 5.3 Auth WebSocket con token HMAC stateless (`main.py:79-96`)

**Il problema:** chiunque conosca l'URL `/media-stream` potrebbe aprirci un WebSocket.
Twilio però non manda header custom sul WS — passa i parametri solo nell'evento `start`.

**La soluzione:** in `/incoming-call` conio un token = `timestamp.nonce.HMAC(secret, ts:nonce)`
con TTL 30s, lo passo come `<Parameter>` dello Stream, e lo **verifico** quando arriva nel
`start`. Niente sessioni da memorizzare → **funziona con più worker** (stateless).

🎤 *"Perché HMAC e non un token in DB?"* → "Stateless: non devo condividere storage tra le
istanze. Il segreto condiviso (`WS_TOKEN_SECRET`) basta a tutti i worker per verificare. TTL
corto perché il token serve solo nei secondi tra `/incoming-call` e l'apertura del WS."

### 5.4 Regole di business nel codice, non nell'LLM

**Regole recuperi** (`supabase_tools.py`, `RECOVERY_RULES`):
```python
{"intermedio": ["base"], "avanzato": ["intermedio", "base"], "base": []}
```
Un intermedio recupera solo in base; un avanzato in intermedio/base; un base non recupera.

**Capacità corsi:** prima di inserire una prenotazione, conto i `confirmed` e rifiuto se
`>= max_capacity`. **E** c'è un **trigger PostgreSQL** che lo impedisce a livello DB (rete di
sicurezza contro race condition / TOCTOU).

🎤 *"Perché non lasci decidere all'LLM?"* → "Perché l'LLM può allucinare e un cliente può
provare a manipolarlo a parole ('fammi recuperare in avanzato'). Le regole sono **invarianti
di policy**, non dati: in codice sono versionate, sempre applicate lato server, e verificabili.
La capacità ha pure il controllo a livello DB così due prenotazioni in gara non sforano."

### 5.5 Intento della chiamata derivato dai tool, non dal testo (`main.py:705-725`)

Alla chiusura, `intent_detected` e `outcome` si calcolano dall'**insieme dei tool chiamati**
(`tools_called`), con priorità `escalation > prenotazione > recupero > info_corsi > unknown`.

🎤 *"Perché non parsare la risposta dell'LLM?"* → "Perché il testo è non-deterministico e
allucinabile. I tool chiamati sono un **fatto**: se è partito `create_booking`, una prenotazione
è stata fatta, punto. È deterministico, resistente a manipolazione, e affidabile per analytics."

### 5.6 Normalizzazione fonetica STT→DB (`supabase_tools.py`, `_normalize_style`)

**Il problema (vero bug trovato in demo):** "bachata" al telefono viene trascritta "baciata" o
"facciata". Il system prompt per giunta usa grafie fonetiche ("baciata") per far pronunciare
bene il TTS. Risultato: `get_courses(style="baciata")` non trovava nulla (in DB è "Bachata").

**La soluzione a due livelli:**
- Una tabella `_STYLE_ALIASES` mappa le varianti fonetiche → nome canonico, applicata **prima
  della query** SQL.
- Il system prompt ora dice esplicitamente: grafie fonetiche **solo per il parlato**, nei tool
  usa i nomi standard.

🎤 Ottimo aneddoto da raccontare: dimostra che hai **debuggato un problema reale di produzione**
nella catena STT→LLM→DB, non solo scritto codice felice.

### 5.7 Cache TTL sui corsi (`supabase_tools.py`, `_COURSES_CACHE_TTL = 300`)

I corsi cambiano al massimo settimanalmente. `get_courses` cache-a i risultati in memoria 5 min
per combinazione di filtri → niente round-trip ripetuti a Supabase nella stessa chiamata
(quando il cliente fa più domande sullo stesso stile). Da ~100-200ms a <1ms su cache hit.

### 5.8 Streaming frase-per-frase verso il TTS (`main.py:504-514`)

Mentre GPT-4o **streamma** i token, accumulo e taglio sui confini di frase (regex
`(?<=[.!?])\s+`). Appena ho una frase completa, la mando **subito** al TTS senza aspettare la
fine della risposta. → l'agente comincia a parlare prima, latenza percepita molto più bassa.

---

## 6. I tool dell'agente (cosa sa fare GPT)

Definiti in `OPENAI_TOOLS` (`main.py:99-301`), dispatchati in `_dispatch_tool` (`main.py:429-455`).

| Tool | I/O | Note |
|------|-----|------|
| `get_courses` | Supabase | filtri style/level/location/instructor/day; cache; normalizzazione; `day_name` |
| `create_booking` | Supabase | check capacità (app + trigger DB) |
| `create_recovery` | Supabase | regole livello + capacità, in una sola sessione DB |
| `notify_secretary` | Twilio WhatsApp | escalation; wrappato in `to_thread` (client sync) |
| `get_settings` | Supabase | flag globali (es. `trial_week_active`) |
| `check_trial_used` | Supabase | una prova gratis per corso |
| `create_trial_session` | Supabase | registra la prova |
| `get_pricing` | **funzione pura** | nessun I/O: 1° corso €160, extra €128 (−20%) |

**Dispatch parallelo:** se GPT chiede più tool nello stesso turno, vengono eseguiti **concorrentemente**
con `asyncio.gather` (`main.py:529-532`).

---

## 7. ⭐ Il budget di latenza (l'altro tema da voice engineer)

I voice agent si giudicano sulla **latenza end-to-end** (silenzio dell'utente → primo suono
della risposta). La card del ruolo Yellowtech cita esplicitamente "latenza end-to-end,
completion rate, contain rate". Sappi scomporla:

```
Utente smette di parlare
   │
   ├─ (A) Endpointing STT: Deepgram aspetta il silenzio per dire "ha finito"
   │       → governato da utterance_end_ms="1000" (main.py:406)  ~ fino a 1s
   │       ⮕ È la leva di latenza percepita PIÙ GROSSA. Tradeoff: abbassarlo = più
   │         reattivo ma più rischio di tagliare la parola a metà.
   │
   ├─ (B) LLM: time-to-first-token di GPT-4o                      ~ 300-600ms
   │       (+ eventuali round di tool-calling verso Supabase      ~ 100-200ms l'uno)
   │
   ├─ (C) TTS: time-to-first-chunk di ElevenLabs                  ~ 200-400ms
   │
   └─ (D) Conversione + invio primo frame a Twilio               ~ trascurabile
```

**Come la misuro (instrumentata, non a occhio):**
Un dict di timing viaggia attraverso le code (agganciato alla prima frase del turno). Quando
parte il primo frame audio, `tts_sender` logga `[latency] ttft=… tts_ttfb=… total_response=…
tool_rounds=… tool_ms=…`:
- `ttft` = STT-final → primo token LLM (= B, include i tool sui turni con tool → `tool_ms` a parte)
- `tts_ttfb` = prima frase → primo chunk ElevenLabs (= C)
- `total_response` = STT-final → primo frame out (= B+C+D, la latenza server-side che possiedo)
- A fine chiamata le medie (`avg_response_ms`, `avg_ttft_ms`, `n_turns`) vengono **persistite in
  `call_logs`** (migration 006) e mostrate nella **dashboard** (KPI Latenza + colonna per chiamata).
Quello che NON misuro è (A) l'endpointing Deepgram e la rete PSTN — fuori dal processo.

**Come la mitigo nel codice:**
- **Streaming a ogni livello**: STT interim, LLM token-streaming, TTS chunk-streaming.
- **Frase-per-frase**: il TTS parte sulla prima frase, non a fine risposta (§5.8).
- **Cache corsi**: toglie round-trip DB nei tool (§5.7).
- **Tool in parallelo**: `asyncio.gather` invece che in sequenza.

🎤 *"Qual è il tuo collo di bottiglia di latenza?"* → "Lo so perché lo misuro: la latenza
server-side (`total_response`) la loggo e la persisto per chiamata. Il collo di bottiglia
percepito però è a monte di quello che possiedo — l'endpointing STT: aspetto ~1s di silenzio per
essere sicuro che l'utente abbia finito (`utterance_end_ms`). Si potrebbe abbassare o usare
endpointing semantico, al prezzo di tagliare le pause lunghe. Il resto è già tutto in streaming."

---

## 8. Gestione del contesto LLM (`main.py:466-469`)

Ogni turno ricostruisco i `messages` così:
```
[SYSTEM_PROMPT] + [tutti i messaggi system] + [ultimi 20 turni user/assistant]
```
I messaggi **system** (identità studente, contesto prova) sono **sempre tenuti**; solo i turni
user/assistant vengono troncati agli ultimi 20. → il contesto critico non si perde mai, ma la
finestra non cresce all'infinito.

Il **saluto iniziale** viene aggiunto alla `history` come messaggio assistant (`main.py:689`):
così l'LLM sa di essersi già presentato e non si ripresenta (fix del bug "doppio saluto").

Loop di tool-calling: fino a **10 iterazioni** (`main.py:470`); se le esaurisce, manda un
fallback. Timeout di 10s su ogni chiamata a GPT (`main.py:483`).

---

## 9. ⚠️ Punti deboli da saper difendere (onestà = forza)

Meglio che li dica tu con consapevolezza. Sono già nel README ("What would need work"):

- **Stato in memoria, per-connessione**: se il processo riparte a metà chiamata, la chiamata
  si perde. Nessuna continuità tra chiamate. → *Come lo sistemerei: stato in Redis con TTL,
  o un session store esterno.*
- **Keepalive single-process**: il loop assume un solo processo; in multi-worker servirebbe uno
  scheduler dedicato.
- **Error recovery basico**: un tool che fallisce logga e restituisce un messaggio, ma niente
  retry/circuit-breaker. → *Aggiungerei retry con backoff sui tool idempotenti.*
- **Test: c'è una eval suite riproducibile, non unit test.** `evals/run_evals.py` esegue scenari
  fissi sul vero prompt+tool+modello e misura task-success (100% su 6 scenari, p50 4.2s/p95 6.3s),
  con trend tra run in dashboard. → *Mancano ancora unit test sul layer tool (mock Supabase) e un
  test di integrazione sul parsing degli eventi Twilio / pipeline audio.*
- **Osservabilità: buona, non completa.** Latenza per-turno persistita in `turn_metrics`, rollup +
  costo in `call_traces`, viste `/observability` (per-stage, p50/p95, costo, barge-in) e `/evals`.
  → *Mancano ancora: tracing distribuito e alerting su soglie. Li aggiungerei con OpenTelemetry.*
- **Niente admin UI per la config nel backend**: orari e settings si toccano in Supabase (la
  dashboard Next.js separata copre la parte analytics/monitoring, non l'editing dei corsi).
- **Cold start su Render free** (vedi §11): la prima chiamata dopo inattività può cadere.

🎤 *"Cosa miglioreresti?"* → scegli 2-3 di questi e proponi la soluzione. Mostra maturità.

---

## 10. Glossario lampo (termini che potrebbero usare)

- **PSTN**: la rete telefonica pubblica tradizionale.
- **mulaw / μ-law**: codifica audio 8-bit/8kHz della telefonia.
- **STT / TTS**: Speech-to-Text / Text-to-Speech.
- **VAD**: Voice Activity Detection (rileva *che* c'è voce, non *cosa* dice).
- **Barge-in**: la capacità dell'utente di interrompere l'agente che parla.
- **Endpointing**: decidere quando l'utente ha *finito* di parlare.
- **Interim / final transcript**: trascrizione parziale in corso / definitiva.
- **Tool calling / function calling**: l'LLM che invoca funzioni strutturate.
- **TOCTOU**: Time-Of-Check-To-Time-Of-Use, la race condition che il trigger DB previene.
- **Backpressure**: quando un consumatore lento rallenta il produttore (le code lo gestiscono).
- **TwiML**: il dialetto XML con cui istruisci Twilio (`<Connect><Stream>`).
- **HMAC**: firma con chiave segreta per autenticare/verificare integrità.

---

## 11. Il "cold start" della prima chiamata (operativo)

**Sintomo:** la prima chiamata dopo un po' di inattività non parte; un minuto dopo va.

**Causa:** Render piano free **spegne il container dopo ~15 min** senza traffico HTTP in
ingresso. La prima chiamata deve risvegliarlo (~30-60s di boot), ma Twilio ha un **timeout sul
webhook (~15s)** → cade. Il keepalive interno (`main.py:39-47`) pinga **Supabase**, non Render,
quindi non c'entra.

**Fix scelto:** un **pinger esterno** che chiama `GET /health` ogni ~10 min tiene il servizio
sempre caldo. (Setup nei passi che ti ho dato in chat.) In alternativa: upgrade Render Starter
(€7/mese, niente spin-down).

🎤 Se a colloquio ti chiedono di deployment, questo è un bel punto: dimostra che capisci la
differenza tra **attività interna del processo** e **traffico HTTP in ingresso** che i PaaS
usano per misurare l'idle.

**Altro gotcha di deploy (buon aneddoto):** il deploy è esploso con "Port scan timeout — no open
ports detected". Causa: `audioop` (usato per il resample/μ-law del TTS) è stato **rimosso dalla
stdlib in Python 3.13** (PEP 594), e l'immagine di default di Render è passata a 3.13+ → l'app
crashava all'import → uvicorn non faceva il bind. Fix: il backport `audioop-lts` in
`requirements.txt` con marker `python_version >= "3.13"`. Lezione: "port scan timeout" su un PaaS
≈ l'app crasha prima del bind — quasi sempre un errore di import, non un problema di porta.

---

## 12. Checklist "so disegnarlo alla lavagna"

Prima del colloquio, verifica di saper fare senza guardare:
- [ ] Il diagramma dei 3 task + le 3 code + il main loop (§4).
- [ ] La sequenza `/incoming-call` → TwiML → WebSocket → `start` → saluto (§3).
- [ ] I 4 passi del barge-in + perché interim e non VAD (§5.1).
- [ ] La catena di conversione audio 24kHz PCM → 8kHz mulaw → frame 160B (§5.2).
- [ ] Il budget di latenza A-B-C-D e qual è il collo di bottiglia (§7).
- [ ] 3 decisioni "lato server vs LLM" e perché (§5.4, §5.5).
- [ ] 3 punti deboli con la rispettiva soluzione (§9).

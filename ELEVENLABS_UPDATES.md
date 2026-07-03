<!-- last-check: 2026-07-03 -->
# ElevenLabs — aggiornamenti & best practice per Dance Voice Agent
_Ultimo check: 2026-07-03 · Stack: **TTS API diretta** (no Conversational AI platform) · Modello: `eleven_flash_v2_5` · output: `ulaw_8000` nativo Twilio via WS multi-context + `auto_mode` · Lingua: it/es/en dinamica_

> ✅ **2026-07-03 — le 3 raccomandazioni del check 2026-06-30 sono APPLICATE** su branch
> `feat/elevenlabs-updates` (`7f5dd3e` = flash_v2_5 + ulaw_8000 · `a40c3be` = WS multi-context
> + auto_mode, un context per frase, riconnessione lazy). Misurato con `evals/tts_bench.py`
> (mediana n=6): **TTFB 294→137ms, sintesi totale 1042→209ms**, ~84% banda in meno.
> Eval decision layer post-modifiche: **6/6**. Pending: chiamata reale + merge + deploy Render.
> Il testo sotto è il report del check 2026-06-30 (pipeline "attuale" = quella pre-modifiche).

## ⚠️ Finding principale
Il modello in uso, **`eleven_v3`, NON è raccomandato per il realtime/telefonia**: ElevenLabs indica esplicitamente Flash v2.5 / Flash v2 / Multilingual v2 per gli usi conversational realtime ed **esclude v3 per first-token latency alta** + costo/char più alto. Su una linea PSTN a 8 kHz mulaw la qualità extra di v3 viene comunque buttata dal band-limiting del telefono → stai pagando (latenza + crediti) per qualità che il canale non trasmette.

## Best practice correnti (stato dell'arte per QUESTO stack, giu 2026)
- **Modello: `eleven_flash_v2_5`** per il realtime — ~75 ms di inference, 32 lingue (it/es/en incluse), **~50% costo/char in meno** vs i modelli di qualità. È il modello consigliato per voice agent/telefonia. (`eleven_turbo_v2_5` è deprecato → equivalente ma più lento di Flash: non usarlo.)
- **`output_format="ulaw_8000"`** invece di `pcm_24000`+resample: ElevenLabs restituisce direttamente μ-law 8 kHz (formato nativo Twilio). **Elimina `audioop.ratecv` + `audioop.lin2ulaw`** e quindi la dipendenza `audioop`/`audioop-lts` (quella che ha causato il crash Python 3.13). Meno CPU per chunk, meno latenza, una dipendenza fragile in meno.
- **Streaming a TTFB minimo: WebSocket endpoint + `auto_mode=true`.** Il WS bidirezionale è il path consigliato per input testo in streaming dall'LLM (~100-150 ms TTFB EU/US/SEA); `auto_mode` gestisce i trigger di generazione e toglie la gestione manuale dei chunk. (Il vecchio `optimize_streaming_latency` è **deprecato** → sostituito da `auto_mode`.) Lo streaming non riduce l'inference, ma abbatte la latenza *percepita*.
- **Voce**: default/synthetic/Instant Voice Clone, **non** Professional Voice Clone (PVC più lenta).
- **Region** vicina agli utenti (EU) per il TTFB.
- **`language_code` dinamico**: tenerlo — Flash v2.5 supporta l'enforcement della lingua, quindi lo switch da v3 a flash non rompe l'i18n it/es/en.

## Novità rilevanti di questo giro (baseline)
| Feature | Stato | Valore | Costo | Dove serve |
|---------|:----:|:------:|------|------------|
| Modello realtime → `eleven_flash_v2_5` (v3 escluso dal realtime) | GA | **alto** | ↓ ~50%/char | [main.py:466](main.py) `model_id` |
| `output_format="ulaw_8000"` (no resample, no audioop) | GA | **alto** | neutro | [main.py:463-496](main.py) elimina `audioop.ratecv`/`lin2ulaw` |
| WebSocket TTS + `auto_mode=true` (TTFB ~100-150ms) | GA | medio | neutro | [main.py:463](main.py) refactor del bridge sync→thread |
| `optimize_streaming_latency` deprecato → `auto_mode` | GA | info | — | non usato oggi: nessuna azione, nuovo default |
| `eleven_v3` ora GA (era alpha) | GA | basso | alto/char | ok per audio **pre-renderizzato** offline (greeting/voicemail), non realtime |
| Deprecati `eleven_monolingual_v1`/`eleven_multilingual_v1` (rimozione **2026-07-09**) | deprecato | housekeeping | — | verificare nessun riferimento residuo (oggi si usa v3 → safe) |

## Non rilevanti ora (perché)
- `expressive_mode`, `search_documentation` system tool, batch-call timezone scheduling, STT entity detection, enum LLM (`claude-opus-4-7`, `gpt-5.4/5.5`, `qwen36`) → feature della **Conversational AI platform** nativa; questo agente è pipeline custom (GPT-4o diretto + Deepgram STT), non l'agente nativo ElevenLabs.
- Scribe v2 (STT), Music v2, video-to-music, nuovi default formati mp3 → modalità diverse / qui STT = Deepgram e output = μ-law per telefono.

## Changelog monitorato (delta)
- **2026-06 (baseline)**: deprecati `eleven_monolingual_v1`/`eleven_multilingual_v1` (rimozione 2026-07-09); nuovi formati mp3 alta qualità (non rilevante). Conferma raccomandazione Flash per realtime; v3 ora GA.

## Fonti
- Models — https://elevenlabs.io/docs/overview/models
- Latency optimization — https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization
- Understanding latency — https://elevenlabs.io/docs/eleven-api/concepts/latency
- Twilio cookbook — https://elevenlabs.io/docs/cookbooks/text-to-speech/twilio
- Changelog — https://elevenlabs.io/docs/changelog

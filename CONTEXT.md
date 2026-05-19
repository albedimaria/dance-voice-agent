# danza-voice-agent — Project Context

## Cos'è

Voice agent inbound per scuola di ballo. Il sistema riceve chiamate telefoniche da studenti e gestisce autonomamente prenotazioni, recuperi e richieste informative. Lingua principale: italiano. Supporto spagnolo pianificato.

Progetto portfolio — dominio fittizio ma architettura production-ready, deployabile su cliente reale.

## Stack

- **Backend:** FastAPI (Python)
- **Telefonia:** Twilio inbound (WebSocket streaming)
- **STT:** Deepgram Nova-2 (streaming, real-time)
- **LLM:** GPT-4o-mini (orchestrator + sub-agents)
- **TTS:** da decidere tra Cartesia Sonic e ElevenLabs Turbo v2.5 — test qualità italiano in corso
- **DB:** Supabase (PostgreSQL)
- **Hosting:** Railway (FastAPI), Vercel (eventuale frontend demo)

## Architettura

Pipeline: `Twilio → Deepgram STT → Orchestrator → Sub-agent → Cartesia/ElevenLabs TTS → Twilio`

### Orchestrator
Classifica l'intent della chiamata e smista al sub-agent corretto. Se confidence bassa o richiesta fuori dominio → escalation segreteria via email.

### Sub-agents
- **BookingAgent** — verifica disponibilità, prenota lezione, conferma
- **RecoveryAgent** — applica regole di recupero, trova slot eligibili, prenota
- **InfoAgent** — risponde su corsi, orari, prezzi, istruttori (solo lettura DB)

## Database — Tabelle

### students
| Campo | Tipo | Note |
|---|---|---|
| id | uuid PK | |
| phone | text UNIQUE | chiave di lookup da Twilio |
| first_name | text | |
| last_name | text | |
| level | enum | base / intermedio / avanzato |
| level_verified | boolean | false se profilo creato al volo durante chiamata |
| active_subscription | boolean | |
| language_preference | enum | it / es, default it |
| created_at | timestamptz | |

### courses
| Campo | Tipo | Note |
|---|---|---|
| id | uuid PK | |
| name | text | es. "Salsa Base", "Bachata Intermedio" |
| level | enum | base / intermedio / avanzato |
| style | text | salsa / bachata / merengue / ... |
| instructor | text | |
| day_of_week | int | 0 = lunedì, 6 = domenica |
| time_start | time | |
| duration_minutes | int | |
| max_capacity | int | |
| active | boolean | |

### bookings
| Campo | Tipo | Note |
|---|---|---|
| id | uuid PK | |
| student_id | uuid FK → students | |
| course_id | uuid FK → courses | |
| date | date | data specifica della lezione |
| type | enum | regular / recovery |
| status | enum | confirmed / cancelled |
| created_at | timestamptz | |

### call_logs
| Campo | Tipo | Note |
|---|---|---|
| id | uuid PK | |
| student_id | uuid nullable FK → students | null se studente non identificato |
| phone_from | text | numero chiamante da Twilio |
| intent_detected | text | |
| outcome | text | |
| escalated | boolean | true se passato a segreteria |
| duration_seconds | int | |
| created_at | timestamptz | |

## Regole di Business

### Recuperi
Encodate come dict in codice, non in DB — logica stabile, non dati.

```python
RECOVERY_RULES = {
    "intermedio": ["base"],
    "avanzato": ["intermedio", "base"],
    "base": []  # nessun recupero disponibile
}
```

### Nuovo caller
Se il numero non è in DB, l'agent raccoglie nome, cognome e livello dichiarato durante la chiamata → crea profilo con `level_verified = false`.

### Escalation
L'orchestratore passa alla segreteria (email) quando:
- Intent non classificabile
- Richiesta fuori dominio
- Errore critico durante la sessione

## Ordine di Build

1. Schema Supabase + seed data fittizi
2. Twilio inbound + WebSocket streaming → FastAPI
3. Deepgram STT — conversation loop base
4. TTS (dopo test qualità) — loop audio end-to-end
5. Orchestrator + sub-agents con tool calls su Supabase
6. Secretary notification (email)
7. Multilingual routing (italiano / spagnolo)

## Istruzioni per Claude Code

- Non scrivere codice non esplicitamente richiesto
- Non aggiungere tabelle, middleware o dipendenze non presenti in questo documento
- Ogni decisione architetturale non banale va discussa prima dell'implementazione
- Usa `DRY_RUN` flag dove applicabile per testare logica senza side effects
- Checkpoint graduali: ogni componente deve funzionare in isolamento prima di integrare il successivo

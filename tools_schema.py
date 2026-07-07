"""OpenAI tool (function-calling) schema for the voice agent.

Extracted from main.py so it can be imported by both the agent and the eval
runner without pulling in the heavy server dependencies (Deepgram, ElevenLabs,
FastAPI). Pure data — no side effects.
"""

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_courses",
            "description": (
                "Recupera i corsi attivi di Ritmo Caliente. "
                "Usa questo tool per verificare disponibilità prima di confermare prenotazioni o recuperi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "style": {
                        "type": "string",
                        "description": "Filtra per stile o nome corso (ricerca parziale, es. 'baciata', 'salsa', 'sensual'). Usa quando l'utente chiede di uno stile specifico.",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["base", "intermedio", "avanzato"],
                        "description": "Filtra per livello. Usa SOLO se l'utente lo chiede esplicitamente — mai come filtro automatico.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Filtra per sede (es. 'AIDA', 'TIGER').",
                    },
                    "instructor": {
                        "type": "string",
                        "description": "Filtra per nome istruttore (ricerca parziale, es. 'Marco', 'Rossi').",
                    },
                    "day": {
                        "type": "string",
                        "description": "Filtra per giorno della settimana. Accetta nomi italiani interi o abbreviati: 'lunedì'/'lun', 'martedì'/'mar', 'mercoledì'/'mer', 'giovedì'/'gio', 'venerdì'/'ven', 'sabato'/'sab', 'domenica'/'dom'. Usa quando il chiamante chiede cosa c'è in un giorno specifico.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": (
                "Prenota una lezione regolare per uno studente. "
                "Chiama SOLO dopo aver confermato corso e data con il chiamante. "
                "Verifica prima la disponibilità con get_courses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente (da get_student_by_phone).",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso (da get_courses).",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data della lezione in formato YYYY-MM-DD.",
                    },
                },
                "required": ["student_id", "course_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_recovery",
            "description": (
                "Prenota un recupero per uno studente in un corso di livello inferiore. "
                "Il sistema verifica automaticamente la compatibilità di livello e la capienza. "
                "Chiama SOLO dopo aver confermato corso e data con il chiamante."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente (da get_student_by_phone).",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso target (da get_courses).",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data del recupero in formato YYYY-MM-DD.",
                    },
                },
                "required": ["student_id", "course_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_secretary",
            "description": (
                "Invia un messaggio WhatsApp alla segreteria di Ritmo Caliente. "
                "Usa questo tool quando il chiamante ha un problema che non riesci a risolvere autonomamente "
                "(es. reclami, richieste speciali, pagamenti, situazioni fuori dalla tua competenza)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Descrizione chiara del problema o della richiesta del chiamante.",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_settings",
            "description": (
                "Legge le impostazioni globali della scuola (es. 'trial_week_active'). "
                "Usalo per verificare se la settimana di prova è attiva prima di proporre lezioni di prova."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_trial_used",
            "description": (
                "Verifica se uno studente ha già usato la lezione di prova per un corso specifico. "
                "Restituisce true se la prova è già stata usata, false altrimenti."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente.",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso.",
                    },
                },
                "required": ["student_id", "course_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_trial_session",
            "description": (
                "Registra una lezione di prova per uno studente in un corso. "
                "Chiama solo se trial_week_active è true e check_trial_used ha restituito false. "
                "Ogni studente può fare al massimo una prova per corso."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente.",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data della lezione di prova in formato YYYY-MM-DD.",
                    },
                },
                "required": ["student_id", "course_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pricing",
            "description": (
                "Calcola il costo dell'abbonamento in base al numero di corsi. "
                "Primo corso €160, ogni corso aggiuntivo €128 (−20%). "
                "Usa quando il chiamante chiede informazioni sui prezzi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_count": {
                        "type": "integer",
                        "description": "Numero di corsi a cui lo studente vuole iscriversi.",
                    },
                },
                "required": ["course_count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_student_bookings",
            "description": (
                "Elenca le prenotazioni future confermate di uno studente (lezioni regolari e recuperi), "
                "con nome corso, data, ora e sede. Chiamalo SEMPRE prima di annullare o spostare una "
                "prenotazione, per trovare il booking_id giusto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente.",
                    },
                },
                "required": ["student_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": (
                "Annulla una prenotazione confermata (lezione regolare o recupero). "
                "Chiama SOLO dopo aver confermato ad alta voce con il chiamante QUALE lezione annullare. "
                "Per spostare una lezione: prima cancel_booking, poi create_booking sulla nuova data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "UUID della prenotazione (da get_student_bookings).",
                    },
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente proprietario della prenotazione.",
                    },
                },
                "required": ["booking_id", "student_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_faq",
            "description": (
                "Recupera le FAQ della scuola (orari segreteria, parcheggio, pagamenti, lezioni private, "
                "abbigliamento, età minima, eventi, partner). Chiamalo PRIMA di dire 'non ho questa "
                "informazione' su domande pratiche fuori dai corsi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Argomento opzionale per filtrare (es. 'parcheggio'). Ometti per avere tutte le FAQ.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_secretary",
            "description": (
                "Trasferisce la telefonata IN DIRETTA alla segreteria (una persona vera). "
                "Usalo quando il chiamante è agitato, chiede esplicitamente di parlare con una persona, "
                "o il problema è troppo complesso. Prima di chiamarlo dì una frase breve tipo "
                "'Ti metto in contatto con la segreteria, un attimo'. "
                "Diverso da notify_secretary (che manda solo un messaggio): questo passa la chiamata ORA."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": (
                "Chiude la telefonata. Chiamalo SOLO quando la conversazione è davvero conclusa "
                "(il chiamante ha ottenuto ciò che voleva o ti ha salutato). Prima di chiamarlo, "
                "dì una frase di saluto breve; poi la chiamata si chiude da sola."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

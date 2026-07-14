import asyncio
from datetime import date as date_type, datetime
import os
import time

from supabase import Client
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient


_DAY_NAMES_IT: list[str] = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_DAY_ALIASES: dict[str, int] = {
    "lun": 0, "lunedì": 0, "lunedi": 0,
    "mar": 1, "martedì": 1, "martedi": 1,
    "mer": 2, "mercoledì": 2, "mercoledi": 2,
    "gio": 3, "giovedì": 3, "giovedi": 3,
    "ven": 4, "venerdì": 4, "venerdi": 4,
    "sab": 5, "sabato": 5,
    "dom": 6, "domenica": 6,
}

# Corsi cambiano raramente: cache con TTL di 5 minuti per ridurre latenza
_courses_cache: dict[str, tuple[list[dict], float]] = {}
_COURSES_CACHE_TTL: float = 300.0


def _courses_cache_key(style: str | None, level: str | None, location: str | None, instructor: str | None, day_num: int | None) -> str:
    return f"{style or ''}|{level or ''}|{location or ''}|{instructor or ''}|{'' if day_num is None else day_num}"


def _validate_date(date_str: str) -> str | None:
    """Ritorna un messaggio di errore se la data non è valida, None se ok."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return f"Data non valida: '{date_str}'. Usa il formato YYYY-MM-DD (es. 2026-06-15)."
    today = date_type.today()
    if d < today:
        return f"Non puoi prenotare per una data già passata ({date_str})."
    if (d - today).days > 180:
        return "Non puoi prenotare con più di 6 mesi di anticipo."
    return None


async def get_student_by_phone(supabase: Client, phone: str) -> dict | None:
    def _query():
        result = (
            supabase.table("students")
            .select("id, first_name, last_name, level, active_subscription, language_preference")
            .eq("phone", phone)
            .maybe_single()
            .execute()
        )
        return result.data

    return await asyncio.to_thread(_query)


# Mappa grafie fonetiche → nomi standard nei DB.
# L'STT può trascrivere "bachata" come "baciata"/"facciata" ecc.;
# il system prompt usa le grafie fonetiche per il TTS ma get_courses
# deve cercare i nomi reali nel database.
_STYLE_ALIASES: dict[str, str] = {
    "baciata": "bachata",
    "bachiata": "bachata",
    "facciata": "bachata",
    "faciata": "bachata",
    "bacciata": "bachata",
    "merenghe": "merengue",
    "merenghè": "merengue",
    "merengè": "merengue",
    "regghetòn": "reggaeton",
    "regghetón": "reggaeton",
    "reggheton": "reggaeton",
    "reggeton": "reggaeton",
}


def _normalize_style(raw: str) -> str:
    """Sostituisce grafie fonetiche con i nomi standard per la ricerca in DB."""
    s = raw.lower().strip()
    for alias, canonical in _STYLE_ALIASES.items():
        s = s.replace(alias, canonical)
    return s


async def get_courses(
    supabase: Client,
    style: str | None = None,
    level: str | None = None,
    location: str | None = None,
    instructor: str | None = None,
    day: str | None = None,
) -> list[dict]:
    # Risolvi il giorno in intero se specificato
    day_num: int | None = None
    if day is not None:
        raw = day.strip().lower()
        if raw.isdigit():
            day_num = int(raw)
        else:
            day_num = _DAY_ALIASES.get(raw)

    cache_key = _courses_cache_key(style, level, location, instructor, day_num)
    now = time.monotonic()
    if cache_key in _courses_cache:
        cached, cached_at = _courses_cache[cache_key]
        if now - cached_at < _COURSES_CACHE_TTL:
            return cached

    def _query():
        q = (
            supabase.table("courses")
            .select("id, name, style, level, instructor, day_of_week, time_start, duration_minutes, max_capacity, location")
            .eq("active", True)
        )
        if style:
            normalized = _normalize_style(style)
            # cerca in entrambe le colonne style e name
            q = q.or_(f"style.ilike.%{normalized}%,name.ilike.%{normalized}%")
        if level:
            q = q.eq("level", level)
        if location:
            q = q.ilike("location", f"%{location}%")
        if instructor:
            q = q.ilike("instructor", f"%{instructor}%")
        if day_num is not None:
            q = q.eq("day_of_week", day_num)
        result = q.order("day_of_week").order("time_start").execute()
        rows = result.data or []
        for row in rows:
            dow = row.get("day_of_week")
            row["day_name"] = _DAY_NAMES_IT[dow] if dow is not None and 0 <= dow <= 6 else "?"
        return rows

    result = await asyncio.to_thread(_query)
    _courses_cache[cache_key] = (result, now)
    return result


async def create_booking(
    supabase: Client,
    student_id: str,
    course_id: str,
    date: str,
) -> dict:
    err = _validate_date(date)
    if err:
        return {"error": err}

    def _query():
        course_result = (
            supabase.table("courses")
            .select("max_capacity, name")
            .eq("id", course_id)
            .maybe_single()
            .execute()
        )
        course = course_result.data
        if not course:
            return {"error": "Corso non trovato."}

        count_result = (
            supabase.table("bookings")
            .select("id", count="exact")
            .eq("course_id", course_id)
            .eq("date", date)
            .eq("status", "confirmed")
            .execute()
        )
        confirmed = count_result.count or 0
        if confirmed >= course["max_capacity"]:
            return {
                "error": (
                    f"Il corso '{course['name']}' è al completo per il {date} "
                    f"({confirmed}/{course['max_capacity']} posti occupati)."
                )
            }

        try:
            insert_result = (
                supabase.table("bookings")
                .insert({
                    "student_id": student_id,
                    "course_id": course_id,
                    "date": date,
                    "type": "regular",
                    "status": "confirmed",
                })
                .execute()
            )
            return insert_result.data[0] if insert_result.data else {"error": "Inserimento fallito."}
        except Exception as exc:
            msg = str(exc)
            if "capacity_exceeded" in msg:
                return {"error": f"Il corso '{course['name']}' è al completo per il {date}."}
            if "bookings_no_duplicate_confirmed" in msg:
                return {"error": "Hai già una prenotazione per questo corso in questa data."}
            return {"error": msg}

    return await asyncio.to_thread(_query)


async def get_student_bookings(supabase: Client, student_id: str) -> list[dict]:
    """Prenotazioni future confermate dello studente (regular + recovery),
    con i dettagli del corso — serve all'agente per disdette e spostamenti."""

    def _query():
        result = (
            supabase.table("bookings")
            .select("id, date, type, courses(name, time_start, location)")
            .eq("student_id", student_id)
            .eq("status", "confirmed")
            .gte("date", date_type.today().isoformat())
            .order("date")
            .execute()
        )
        rows = result.data or []
        # Flatten the course join so the LLM sees one simple dict per booking.
        out = []
        for row in rows:
            course = row.pop("courses", None) or {}
            out.append({
                "booking_id": row["id"],
                "date": row["date"],
                "type": row["type"],
                "course_name": course.get("name"),
                "time_start": course.get("time_start"),
                "location": course.get("location"),
            })
        return out

    return await asyncio.to_thread(_query)


async def cancel_booking(supabase: Client, booking_id: str, student_id: str) -> dict:
    """Annulla una prenotazione (status → cancelled). Solo prenotazioni proprie,
    confermate e non passate — il posto torna disponibile grazie all'indice
    parziale su status='confirmed'."""

    def _query():
        booking_result = (
            supabase.table("bookings")
            .select("id, date, status, student_id, courses(name)")
            .eq("id", booking_id)
            .maybe_single()
            .execute()
        )
        booking = booking_result.data
        if not booking:
            return {"error": "Prenotazione non trovata."}
        if booking["student_id"] != student_id:
            return {"error": "Questa prenotazione non appartiene allo studente."}
        if booking["status"] != "confirmed":
            return {"error": "La prenotazione risulta già annullata."}
        if booking["date"] < date_type.today().isoformat():
            return {"error": "Non si può annullare una lezione già passata."}

        # The extra status filter makes the update atomic: if a concurrent
        # request cancelled it first, this touches 0 rows and stays idempotent.
        supabase.table("bookings").update({"status": "cancelled"}) \
            .eq("id", booking_id).eq("status", "confirmed").execute()
        course = booking.get("courses") or {}
        return {
            "cancelled": True,
            "course_name": course.get("name"),
            "date": booking["date"],
        }

    return await asyncio.to_thread(_query)


# FAQ cambiano raramente: stessa strategia di cache dei corsi.
_faq_cache: tuple[list[dict], float] | None = None


async def get_faq(supabase: Client, topic: str | None = None) -> list[dict]:
    """Restituisce le FAQ attive della scuola. Senza filtro torna tutte le voci
    (tabella piccola): è l'LLM a scegliere la risposta giusta, così le query
    distorte dall'STT non rompono un keyword-match."""
    global _faq_cache

    def _query():
        result = (
            supabase.table("faqs")
            .select("topic, question, answer")
            .eq("active", True)
            .execute()
        )
        return result.data or []

    now = time.monotonic()
    if _faq_cache is not None and now - _faq_cache[1] < _COURSES_CACHE_TTL:
        rows = _faq_cache[0]
    else:
        rows = await asyncio.to_thread(_query)
        _faq_cache = (rows, now)

    if topic:
        t = topic.lower().strip()
        filtered = [r for r in rows if t in r["topic"].lower() or t in r["question"].lower()]
        return filtered or rows  # match vuoto → torna tutto, decide l'LLM
    return rows


async def send_booking_confirmation_sms(
    supabase: Client,
    twilio_client: TwilioClient,
    caller_phone: str,
    course_id: str,
    date: str,
    kind: str,  # 'prenotazione' | 'recupero' | 'lezione di prova'
) -> None:
    """Conferma via SMS dopo una prenotazione riuscita. Deterministica (non
    decisa dall'LLM) e fire-and-forget: un errore SMS non deve mai toccare la
    chiamata in corso."""
    sms_from = os.environ.get("TWILIO_PHONE_NUMBER", "")
    if not sms_from or not caller_phone:
        print("[sms] TWILIO_PHONE_NUMBER o numero chiamante mancanti — conferma saltata")
        return

    def _send():
        course_result = (
            supabase.table("courses")
            .select("name, time_start, location")
            .eq("id", course_id)
            .maybe_single()
            .execute()
        )
        course = course_result.data or {}
        time_start = (course.get("time_start") or "")[:5]
        body = (
            f"Ritmo Caliente — {kind} confermata: "
            f"{course.get('name', 'lezione')} il {date}"
        )
        if time_start:
            body += f" alle {time_start}"
        location = course.get("location")
        if location:
            body += f", Studio {location}"
        body += ". A presto!"
        twilio_client.messages.create(from_=sms_from, to=caller_phone, body=body)

    try:
        await asyncio.to_thread(_send)
        print(f"[sms] conferma {kind} inviata a {caller_phone}")
    except Exception as exc:
        print(f"[sms] invio conferma fallito (non bloccante): {exc}")


async def notify_secretary(message: str, caller_phone: str, twilio_client: TwilioClient) -> dict:
    def _send():
        try:
            twilio_client.messages.create(
                from_=os.environ["TWILIO_WHATSAPP_FROM"],
                to=os.environ["SECRETARY_WHATSAPP"],
                body=f"Chiamata da {caller_phone}:\n{message}",
            )
            return {"sent": True}
        except TwilioRestException as exc:
            print(f"[notify_secretary] Twilio error {exc.status}: {exc.msg}")
            return {
                "sent": False,
                "error": (
                    f"Non sono riuscita ad avvisare la segreteria automaticamente. "
                    f"Puoi contattarla direttamente su WhatsApp al 351 000 0000."
                ),
            }
        except Exception as exc:
            print(f"[notify_secretary] errore generico: {exc}")
            return {
                "sent": False,
                "error": (
                    "Non sono riuscita ad avvisare la segreteria automaticamente. "
                    "Puoi contattarla direttamente su WhatsApp al 351 000 0000."
                ),
            }

    return await asyncio.to_thread(_send)


RECOVERY_RULES: dict[str, list[str]] = {
    "intermedio": ["base"],
    "avanzato": ["intermedio", "base"],
    "base": [],
}


async def get_settings(supabase: Client) -> dict:
    def _query():
        result = supabase.table("settings").select("key, value").execute()
        return {row["key"]: row["value"] for row in (result.data or [])}

    return await asyncio.to_thread(_query)


async def check_trial_used(supabase: Client, student_id: str, course_id: str) -> bool:
    def _query():
        result = (
            supabase.table("trial_sessions")
            .select("id", count="exact")
            .eq("student_id", student_id)
            .eq("course_id", course_id)
            .execute()
        )
        return (result.count or 0) > 0

    return await asyncio.to_thread(_query)


async def create_trial_session(
    supabase: Client,
    student_id: str,
    course_id: str,
    date: str,
) -> dict:
    err = _validate_date(date)
    if err:
        return {"error": err}

    def _query():
        try:
            result = (
                supabase.table("trial_sessions")
                .insert({
                    "student_id": student_id,
                    "course_id": course_id,
                    "date": date,
                })
                .execute()
            )
            return result.data[0] if result.data else {"error": "Inserimento fallito."}
        except Exception as exc:
            msg = str(exc)
            if "trial_sessions_student_course_unique" in msg or "unique" in msg.lower():
                return {"error": "Lo studente ha già usato la lezione di prova per questo corso."}
            return {"error": msg}

    return await asyncio.to_thread(_query)


def get_pricing(course_count: int) -> dict:
    if course_count <= 0:
        return {"error": "course_count deve essere almeno 1."}
    base = 160
    additional = 120
    total = base + (course_count - 1) * additional
    breakdown = [base] + [additional] * (course_count - 1)
    return {
        "total": total,
        "currency": "EUR",
        "course_count": course_count,
        "period": "quadrimestre",
        "lessons_per_course": 16,
        "breakdown": breakdown,
        "note": (
            f"Primo corso €{base} a quadrimestre, 16 lezioni garantite. "
            f"Ogni corso aggiuntivo €{additional} a quadrimestre, sempre 16 lezioni."
        ),
    }


async def create_recovery(
    supabase: Client,
    student_id: str,
    course_id: str,
    date: str,
) -> dict:
    err = _validate_date(date)
    if err:
        return {"error": err}

    def _query():
        student_result = (
            supabase.table("students")
            .select("level, first_name")
            .eq("id", student_id)
            .maybe_single()
            .execute()
        )
        student = student_result.data
        if not student:
            return {"error": "Studente non trovato."}

        course_result = (
            supabase.table("courses")
            .select("level, name, max_capacity")
            .eq("id", course_id)
            .maybe_single()
            .execute()
        )
        course = course_result.data
        if not course:
            return {"error": "Corso non trovato."}

        student_level = student["level"]
        course_level = course["level"]
        allowed = RECOVERY_RULES.get(student_level, [])
        if course_level not in allowed:
            if not allowed:
                return {
                    "error": (
                        f"Gli studenti di livello {student_level} non possono fare recuperi "
                        f"— il livello base non ha corsi di livello inferiore."
                    )
                }
            return {
                "error": (
                    f"Livello incompatibile: uno studente {student_level} può recuperare solo "
                    f"in corsi {' o '.join(allowed)}, non in {course_level}."
                )
            }

        count_result = (
            supabase.table("bookings")
            .select("id", count="exact")
            .eq("course_id", course_id)
            .eq("date", date)
            .eq("status", "confirmed")
            .execute()
        )
        confirmed = count_result.count or 0
        if confirmed >= course["max_capacity"]:
            return {
                "error": (
                    f"Il corso '{course['name']}' è al completo per il {date} "
                    f"({confirmed}/{course['max_capacity']} posti occupati)."
                )
            }

        try:
            insert_result = (
                supabase.table("bookings")
                .insert({
                    "student_id": student_id,
                    "course_id": course_id,
                    "date": date,
                    "type": "recovery",
                    "status": "confirmed",
                })
                .execute()
            )
            return insert_result.data[0] if insert_result.data else {"error": "Inserimento fallito."}
        except Exception as exc:
            msg = str(exc)
            if "capacity_exceeded" in msg:
                return {"error": f"Il corso '{course['name']}' è al completo per il {date}."}
            if "bookings_no_duplicate_confirmed" in msg:
                return {"error": "Hai già un recupero per questo corso in questa data."}
            return {"error": msg}

    return await asyncio.to_thread(_query)

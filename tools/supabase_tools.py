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
    additional = 128
    total = base + (course_count - 1) * additional
    breakdown = [base] + [additional] * (course_count - 1)
    return {
        "total": total,
        "currency": "EUR",
        "course_count": course_count,
        "breakdown": breakdown,
        "note": f"Primo corso €{base}, ogni corso aggiuntivo €{additional} (−20%).",
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

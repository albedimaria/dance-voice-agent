import asyncio
import os

from supabase import Client
from twilio.rest import Client as TwilioClient


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


async def get_courses(
    supabase: Client,
    level: str | None = None,
    location: str | None = None,
    instructor: str | None = None,
) -> list[dict]:
    def _query():
        q = (
            supabase.table("courses")
            .select("id, name, style, level, instructor, day_of_week, time_start, duration_minutes, max_capacity, location")
            .eq("active", True)
        )
        if level:
            q = q.eq("level", level)
        if location:
            q = q.ilike("location", f"%{location}%")
        if instructor:
            q = q.ilike("instructor", f"%{instructor}%")
        result = q.order("day_of_week").order("time_start").execute()
        return result.data or []

    return await asyncio.to_thread(_query)


async def create_booking(
    supabase: Client,
    student_id: str,
    course_id: str,
    date: str,
) -> dict:
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
        twilio_client.messages.create(
            from_=os.environ["TWILIO_WHATSAPP_FROM"],
            to=os.environ["SECRETARY_WHATSAPP"],
            body=f"Chiamata da {caller_phone}:\n{message}",
        )
        return {"sent": True}

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

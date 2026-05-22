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
            q = q.eq("location", location)
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

    return await asyncio.to_thread(_query)


async def notify_secretary(message: str, caller_phone: str) -> dict:
    def _send():
        client = TwilioClient(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
        client.messages.create(
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

    return await asyncio.to_thread(_query)

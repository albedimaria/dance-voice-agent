import asyncio

from supabase import Client


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

"""Pre-lesson SMS reminders — reduces no-shows.

Finds tomorrow's confirmed bookings (regular + recovery) and trial sessions,
and texts each student a reminder. Designed to run once a day from a cron
(GitHub Actions — see .github/workflows/send-reminders.yml), independent of
the voice-agent server so a Render spin-down can't skip a day.

Idempotent: every sent reminder is recorded in reminders_log with a
UNIQUE(kind, booking_id) constraint, so re-runs and overlapping schedules
never text the same student twice for the same lesson.

Env (same .env as the agent): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER.

Run from the repo root:  python -m scripts.send_reminders
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from supabase import create_client
from twilio.rest import Client as TwilioClient

TZ = ZoneInfo("Europe/Rome")

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)
twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
SMS_FROM = os.environ.get("TWILIO_PHONE_NUMBER", "")

KIND_LABEL = {"regular": "lezione", "recovery": "recupero", "trial": "lezione di prova"}


def _tomorrow() -> str:
    return (datetime.now(TZ).date() + timedelta(days=1)).isoformat()


def _already_sent(kind: str, booking_id: str) -> bool:
    """Claim the reminder atomically: the UNIQUE constraint on reminders_log is
    the idempotency guard — insert first, send only if the insert succeeded."""
    try:
        supabase.table("reminders_log").insert(
            {"kind": kind, "booking_id": booking_id}
        ).execute()
        return False
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            return True
        raise


def _send_sms(phone: str, body: str) -> None:
    twilio.messages.create(from_=SMS_FROM, to=phone, body=body)


def _reminder_body(first_name: str, kind_label: str, course: dict, date: str) -> str:
    time_start = (course.get("time_start") or "")[:5]
    body = f"Ciao {first_name}! Promemoria Ritmo Caliente: domani {date} hai {kind_label} di {course.get('name', 'ballo')}"
    if time_start:
        body += f" alle {time_start}"
    if course.get("location"):
        body += f", Studio {course['location']}"
    body += ". Ti aspettiamo!"
    return body


def main() -> int:
    if not SMS_FROM:
        print("[reminders] TWILIO_PHONE_NUMBER mancante — esco")
        return 1

    tomorrow = _tomorrow()
    sent = skipped = failed = 0

    # Confirmed bookings (regular + recovery) for tomorrow.
    bookings = (
        supabase.table("bookings")
        .select("id, date, type, students(first_name, phone), courses(name, time_start, location)")
        .eq("status", "confirmed")
        .eq("date", tomorrow)
        .execute()
    ).data or []

    # Trial sessions for tomorrow (separate table, no status column).
    trials = (
        supabase.table("trial_sessions")
        .select("id, date, students(first_name, phone), courses(name, time_start, location)")
        .eq("date", tomorrow)
        .execute()
    ).data or []

    jobs = [("booking", b, KIND_LABEL.get(b.get("type"), "lezione")) for b in bookings]
    jobs += [("trial", t, KIND_LABEL["trial"]) for t in trials]

    print(f"[reminders] {tomorrow}: {len(bookings)} prenotazioni + {len(trials)} prove")

    for kind, row, label in jobs:
        student = row.get("students") or {}
        course = row.get("courses") or {}
        phone = student.get("phone")
        if not phone:
            skipped += 1
            continue
        if _already_sent(kind, row["id"]):
            skipped += 1
            continue
        try:
            _send_sms(phone, _reminder_body(student.get("first_name", ""), label, course, tomorrow))
            sent += 1
            print(f"[reminders] inviato a {phone} ({label} {course.get('name')})")
        except Exception as exc:
            failed += 1
            print(f"[reminders] invio fallito a {phone}: {exc}")
            # Release the claim so the next run retries this reminder.
            try:
                supabase.table("reminders_log").delete() \
                    .eq("kind", kind).eq("booking_id", row["id"]).execute()
            except Exception as exc2:
                print(f"[reminders] release claim fallita ({kind}/{row['id']}): {exc2}")

    print(f"[reminders] fatti: {sent} inviati, {skipped} saltati, {failed} falliti")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

-- 004_booking_constraints.sql

-- Prevent the same student from booking the same (course, date, type) twice.
-- Partial index covers only active bookings so cancelled slots stay re-bookable.
CREATE UNIQUE INDEX IF NOT EXISTS bookings_no_duplicate_confirmed
    ON bookings (student_id, course_id, date, type)
    WHERE status = 'confirmed';

-- trial_sessions already has this constraint from migration 003; guard with a check.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'trial_sessions_student_course_unique'
    ) THEN
        ALTER TABLE trial_sessions
            ADD CONSTRAINT trial_sessions_student_course_unique
            UNIQUE (student_id, course_id);
    END IF;
END $$;

-- Enforce max_capacity atomically inside the INSERT transaction.
-- Because the count and the guard run in the same DB transaction as the INSERT,
-- no concurrent booking can sneak past the check (no TOCTOU window).
CREATE OR REPLACE FUNCTION check_booking_capacity()
RETURNS TRIGGER AS $$
DECLARE
    v_max_capacity int;
    v_confirmed    int;
BEGIN
    -- Only enforce for confirmed bookings; cancelled inserts pass through.
    IF NEW.status <> 'confirmed' THEN
        RETURN NEW;
    END IF;

    SELECT max_capacity INTO v_max_capacity
    FROM courses
    WHERE id = NEW.course_id;

    SELECT COUNT(*) INTO v_confirmed
    FROM bookings
    WHERE course_id = NEW.course_id
      AND date      = NEW.date
      AND status    = 'confirmed';

    IF v_confirmed >= v_max_capacity THEN
        RAISE EXCEPTION 'capacity_exceeded: corso al completo (% / % posti)',
            v_confirmed, v_max_capacity;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_booking_capacity
    BEFORE INSERT ON bookings
    FOR EACH ROW EXECUTE FUNCTION check_booking_capacity();

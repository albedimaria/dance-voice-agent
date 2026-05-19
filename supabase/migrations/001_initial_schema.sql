-- 001_initial_schema.sql

-- Enums

CREATE TYPE student_level AS ENUM ('base', 'intermedio', 'avanzato');
CREATE TYPE language_preference AS ENUM ('it', 'es');
CREATE TYPE booking_type AS ENUM ('regular', 'recovery');
CREATE TYPE booking_status AS ENUM ('confirmed', 'cancelled');

-- students

CREATE TABLE students (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone                text UNIQUE NOT NULL,
    first_name           text NOT NULL,
    last_name            text NOT NULL,
    level                student_level NOT NULL,
    level_verified       boolean NOT NULL DEFAULT false,
    active_subscription  boolean NOT NULL DEFAULT false,
    language_preference  language_preference NOT NULL DEFAULT 'it',
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- courses

CREATE TABLE courses (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL,
    level            student_level NOT NULL,
    style            text NOT NULL,
    instructor       text NOT NULL,
    day_of_week      int NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    time_start       time NOT NULL,
    duration_minutes int NOT NULL,
    max_capacity     int NOT NULL,
    active           boolean NOT NULL DEFAULT true
);

-- bookings

CREATE TABLE bookings (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id uuid NOT NULL REFERENCES students (id),
    course_id  uuid NOT NULL REFERENCES courses (id),
    date       date NOT NULL,
    type       booking_type NOT NULL,
    status     booking_status NOT NULL DEFAULT 'confirmed',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- call_logs

CREATE TABLE call_logs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id       uuid REFERENCES students (id),
    phone_from       text NOT NULL,
    intent_detected  text,
    outcome          text,
    escalated        boolean NOT NULL DEFAULT false,
    duration_seconds int,
    created_at       timestamptz NOT NULL DEFAULT now()
);

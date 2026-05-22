-- Trial sessions: one trial per student per course
CREATE TABLE trial_sessions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id uuid NOT NULL REFERENCES students(id),
    course_id  uuid NOT NULL REFERENCES courses(id),
    date       date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT trial_sessions_student_course_unique UNIQUE (student_id, course_id)
);

-- Key-value settings store
CREATE TABLE settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value) VALUES ('trial_week_active', 'false');

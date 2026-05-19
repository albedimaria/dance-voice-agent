-- seed.sql — dati fittizi per ambiente di sviluppo

-- courses

INSERT INTO courses (id, name, level, style, instructor, day_of_week, time_start, duration_minutes, max_capacity, active, location) VALUES
    ('a1b2c3d4-0001-0000-0000-000000000001', 'Salsa Base',          'base',       'salsa',   'Marco Russo',    1, '19:00', 60, 12, true, 'milano_ovest'),
    ('a1b2c3d4-0002-0000-0000-000000000002', 'Bachata Intermedio',  'intermedio', 'bachata', 'Laura Ferretti', 3, '20:30', 75, 10, true, 'brugherio'),
    ('a1b2c3d4-0003-0000-0000-000000000003', 'Salsa Avanzato',      'avanzato',   'salsa',   'Marco Russo',    3, '19:00', 90, 8,  true, 'milano_ovest'),
    ('a1b2c3d4-0004-0000-0000-000000000004', 'Merengue Base',       'base',       'merengue','Laura Ferretti', 5, '18:00', 60, 14, true, 'milano_est'),
    ('a1b2c3d4-0005-0000-0000-000000000005', 'Bachata Base',        'base',       'bachata', 'Laura Ferretti', 1, '20:00', 60, 12, true, 'brugherio');

-- students

INSERT INTO students (id, phone, first_name, last_name, level, level_verified, active_subscription, language_preference, created_at) VALUES
    ('b1000000-0000-0000-0000-000000000001', '+393331234567', 'Giulia',    'Marino',    'base',       true,  true,  'it', '2025-09-10 10:00:00+02'),
    ('b1000000-0000-0000-0000-000000000002', '+393472345678', 'Luca',      'Conti',     'intermedio', true,  true,  'it', '2025-09-15 11:30:00+02'),
    ('b1000000-0000-0000-0000-000000000003', '+393613456789', 'Sofia',     'Ricci',     'avanzato',   true,  true,  'it', '2025-08-01 09:00:00+02'),
    ('b1000000-0000-0000-0000-000000000004', '+393204567890', 'Alessandro','Bianchi',   'base',       true,  false, 'it', '2025-10-03 14:00:00+02'),
    ('b1000000-0000-0000-0000-000000000005', '+393395678901', 'Valentina', 'Greco',     'intermedio', true,  true,  'it', '2025-10-20 16:00:00+02'),
    ('b1000000-0000-0000-0000-000000000006', '+393516789012', 'Diego',     'Esposito',  'base',       false, true,  'es', '2026-01-08 10:45:00+01'),
    ('b1000000-0000-0000-0000-000000000007', '+393887890123', 'Martina',   'Lombardi',  'avanzato',   true,  true,  'it', '2025-07-22 09:30:00+02'),
    ('b1000000-0000-0000-0000-000000000008', '+393148901234', 'Emilio',    'Fontana',   'intermedio', false, false, 'es', '2026-03-12 17:20:00+01');

-- bookings
-- regular: studente frequenta il proprio corso di livello
-- recovery: studente avanzato/intermedio recupera su corso di livello inferiore

INSERT INTO bookings (id, student_id, course_id, date, type, status, created_at) VALUES
    -- Giulia (base) → Salsa Base, regular
    ('c1000000-0000-0000-0000-000000000001', 'b1000000-0000-0000-0000-000000000001', 'a1b2c3d4-0001-0000-0000-000000000001', '2026-05-20', 'regular',  'confirmed', '2026-05-15 09:00:00+02'),
    ('c1000000-0000-0000-0000-000000000002', 'b1000000-0000-0000-0000-000000000001', 'a1b2c3d4-0001-0000-0000-000000000001', '2026-05-27', 'regular',  'confirmed', '2026-05-15 09:01:00+02'),

    -- Luca (intermedio) → Bachata Intermedio, regular
    ('c1000000-0000-0000-0000-000000000003', 'b1000000-0000-0000-0000-000000000002', 'a1b2c3d4-0002-0000-0000-000000000002', '2026-05-21', 'regular',  'confirmed', '2026-05-14 18:00:00+02'),

    -- Luca (intermedio) → Salsa Base, recovery
    ('c1000000-0000-0000-0000-000000000004', 'b1000000-0000-0000-0000-000000000002', 'a1b2c3d4-0001-0000-0000-000000000001', '2026-05-13', 'recovery', 'confirmed', '2026-05-12 10:30:00+02'),

    -- Sofia (avanzato) → Salsa Avanzato, regular
    ('c1000000-0000-0000-0000-000000000005', 'b1000000-0000-0000-0000-000000000003', 'a1b2c3d4-0003-0000-0000-000000000003', '2026-05-21', 'regular',  'confirmed', '2026-05-10 11:00:00+02'),

    -- Sofia (avanzato) → Bachata Intermedio, recovery
    ('c1000000-0000-0000-0000-000000000006', 'b1000000-0000-0000-0000-000000000003', 'a1b2c3d4-0002-0000-0000-000000000002', '2026-05-15', 'recovery', 'confirmed', '2026-05-13 09:15:00+02'),

    -- Alessandro (base) → Merengue Base, regular
    ('c1000000-0000-0000-0000-000000000007', 'b1000000-0000-0000-0000-000000000004', 'a1b2c3d4-0004-0000-0000-000000000004', '2026-05-22', 'regular',  'confirmed', '2026-05-16 20:00:00+02'),

    -- Valentina (intermedio) → Bachata Intermedio, regular
    ('c1000000-0000-0000-0000-000000000008', 'b1000000-0000-0000-0000-000000000005', 'a1b2c3d4-0002-0000-0000-000000000002', '2026-05-21', 'regular',  'confirmed', '2026-05-17 10:00:00+02'),

    -- Valentina (intermedio) → Merengue Base, recovery
    ('c1000000-0000-0000-0000-000000000009', 'b1000000-0000-0000-0000-000000000005', 'a1b2c3d4-0004-0000-0000-000000000004', '2026-05-08', 'recovery', 'cancelled', '2026-05-06 15:30:00+02'),

    -- Martina (avanzato) → Salsa Avanzato, regular
    ('c1000000-0000-0000-0000-000000000010', 'b1000000-0000-0000-0000-000000000007', 'a1b2c3d4-0003-0000-0000-000000000003', '2026-05-21', 'regular',  'confirmed', '2026-05-11 08:45:00+02'),

    -- Martina (avanzato) → Salsa Base, recovery
    ('c1000000-0000-0000-0000-000000000011', 'b1000000-0000-0000-0000-000000000007', 'a1b2c3d4-0001-0000-0000-000000000001', '2026-05-06', 'recovery', 'confirmed', '2026-05-05 19:00:00+02'),

    -- Diego (base, level_verified=false) → Salsa Base, regular
    ('c1000000-0000-0000-0000-000000000012', 'b1000000-0000-0000-0000-000000000006', 'a1b2c3d4-0001-0000-0000-000000000001', '2026-05-20', 'regular',  'confirmed', '2026-05-18 21:10:00+02'),

    -- Luca (intermedio bachata) → Bachata Base, recovery
    ('c1000000-0000-0000-0000-000000000013', 'b1000000-0000-0000-0000-000000000002', 'a1b2c3d4-0005-0000-0000-000000000005', '2026-05-27', 'recovery', 'confirmed', '2026-05-19 10:00:00+02'),

    -- Valentina (intermedio bachata) → Bachata Base, recovery
    ('c1000000-0000-0000-0000-000000000014', 'b1000000-0000-0000-0000-000000000005', 'a1b2c3d4-0005-0000-0000-000000000005', '2026-05-20', 'recovery', 'confirmed', '2026-05-19 11:30:00+02');

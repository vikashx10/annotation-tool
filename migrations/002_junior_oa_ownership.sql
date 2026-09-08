-- 002: Move ownership from annotators to Junior OAs
--
-- Supersedes 001. senior_oa_id now records which Senior OA created a JUNIOR OA;
-- a senior can only add Junior OAs they own. Annotator pickers are global again,
-- so any annotator ownership written by 001 is cleared.
--
-- The app performs the same migration automatically on boot
-- (app._backfill_junior_oa_owner). Idempotent: safe to run more than once.

BEGIN;

-- Column already exists if 001 ran; this covers databases that skipped it.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS senior_oa_id INTEGER REFERENCES users(id);

-- 1. Clear annotator ownership written by 001 — no longer consulted anywhere.
UPDATE users
   SET senior_oa_id = NULL
 WHERE role = 'annotator'
   AND senior_oa_id IS NOT NULL
   AND NOT EXISTS (
        SELECT 1 FROM config WHERE key = 'migration:junior_oa_owner_backfill');

-- 2. Hand existing Junior OAs to demo_senior_oa so they stay addable.
UPDATE users
   SET senior_oa_id = (
        SELECT id FROM users
         WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
 WHERE role = 'junior_oa'
   AND senior_oa_id IS NULL
   AND EXISTS (SELECT 1 FROM users WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
   AND NOT EXISTS (
        SELECT 1 FROM config WHERE key = 'migration:junior_oa_owner_backfill');

-- 3. Mark applied so the app skips it on boot.
INSERT INTO config (key, value)
SELECT 'migration:junior_oa_owner_backfill',
       (SELECT id::text FROM users WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
 WHERE EXISTS (SELECT 1 FROM users WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
ON CONFLICT (key) DO NOTHING;

COMMIT;

-- Verify:
--   SELECT u.username, u.role, s.username AS owner
--     FROM users u LEFT JOIN users s ON s.id = u.senior_oa_id
--    WHERE u.role IN ('junior_oa','annotator') ORDER BY u.role, u.username;

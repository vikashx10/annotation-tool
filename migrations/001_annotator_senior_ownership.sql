-- 001: Annotator ownership by Senior OA
--
-- Adds users.senior_oa_id and hands every pre-existing annotator to the
-- Senior OA named below. Idempotent: safe to run more than once.
--
-- The app performs the same migration automatically on boot
-- (models._add_missing_columns + app._backfill_annotator_owner). Run this
-- manually only if you prefer to migrate Supabase ahead of the deploy.

BEGIN;

-- 1. Ownership column. NULL = unowned; unowned annotators are invisible to
--    every Junior OA until an admin assigns an owner.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS senior_oa_id INTEGER REFERENCES users(id);

-- 2. One-time backfill: existing annotators become demo_senior_oa's.
--    Guarded by the config flag so it cannot double-apply.
UPDATE users
   SET senior_oa_id = (
        SELECT id FROM users
         WHERE username = 'demo_senior_oa' AND role = 'senior_oa'
       )
 WHERE role = 'annotator'
   AND senior_oa_id IS NULL
   AND EXISTS (SELECT 1 FROM users WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
   AND NOT EXISTS (
        SELECT 1 FROM config
         WHERE key = 'migration:annotator_senior_owner_backfill'
       );

-- 3. Mark the backfill applied so the app skips it on boot. Without this row
--    the app would sweep any later self-registered annotator into the same
--    senior on the next restart.
INSERT INTO config (key, value)
SELECT 'migration:annotator_senior_owner_backfill',
       (SELECT id::text FROM users WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
 WHERE EXISTS (SELECT 1 FROM users WHERE username = 'demo_senior_oa' AND role = 'senior_oa')
ON CONFLICT (key) DO NOTHING;

COMMIT;

-- Verify:
--   SELECT u.username, s.username AS owner
--     FROM users u LEFT JOIN users s ON s.id = u.senior_oa_id
--    WHERE u.role = 'annotator' ORDER BY u.username;

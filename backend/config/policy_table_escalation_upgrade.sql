-- Escalation mail (BOD / device policy) — production ``policy`` table schema.
-- Prefer: python manage.py migrate userprofile
-- Or:     python manage.py upgrade_policy_schema
--
-- Data-preserving upgrade (no DROP COLUMN). Run via Django when possible.
-- Manual reference DDL below uses ADD/MODIFY only:

ALTER TABLE policy
    ADD COLUMN IF NOT EXISTS subject_category VARCHAR(50) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS device_type VARCHAR(50) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS device_friendly_name VARCHAR(255) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS device_ip VARCHAR(100) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;

ALTER TABLE policy
    MODIFY COLUMN categories LONGTEXT NOT NULL,
    MODIFY COLUMN escalation_mails LONGTEXT NOT NULL,
    MODIFY COLUMN definite_mails LONGTEXT NOT NULL,
    MODIFY COLUMN approval_timer INT NOT NULL DEFAULT 0,
    MODIFY COLUMN resolution_timer INT NOT NULL DEFAULT 0,
    MODIFY COLUMN escalation_required TINYINT(1) NOT NULL DEFAULT 1;

"""
Idempotent, data-preserving MySQL upgrade for the legacy ``policy`` table.

Uses ADD COLUMN (when missing) and MODIFY COLUMN (when type differs) — never
DROP COLUMN, so existing escalation policy rows are kept in production.
"""

import logging

logger = logging.getLogger('linkedeye.policy_schema')

POLICY_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS policy (
    policy_id INT AUTO_INCREMENT PRIMARY KEY,
    subject_category VARCHAR(50) NOT NULL DEFAULT '',
    device_type VARCHAR(50) NOT NULL DEFAULT '',
    device_friendly_name VARCHAR(255) NOT NULL DEFAULT '',
    device_ip VARCHAR(100) NOT NULL DEFAULT '',
    categories LONGTEXT NOT NULL,
    escalation_mails LONGTEXT NOT NULL,
    definite_mails LONGTEXT NOT NULL,
    approval_timer INT NOT NULL DEFAULT 0,
    resolution_timer INT NOT NULL DEFAULT 0,
    escalation_required TINYINT(1) NOT NULL DEFAULT 1,
    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# (column_name, ADD COLUMN ddl, MODIFY COLUMN ddl)
_POLICY_COLUMNS = (
    ('subject_category', "VARCHAR(50) NOT NULL DEFAULT ''", "VARCHAR(50) NOT NULL DEFAULT ''"),
    ('device_type', "VARCHAR(50) NOT NULL DEFAULT ''", "VARCHAR(50) NOT NULL DEFAULT ''"),
    ('device_friendly_name', "VARCHAR(255) NOT NULL DEFAULT ''", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ('device_ip', "VARCHAR(100) NOT NULL DEFAULT ''", "VARCHAR(100) NOT NULL DEFAULT ''"),
    ('categories', 'LONGTEXT NOT NULL', 'LONGTEXT NOT NULL'),
    ('escalation_mails', 'LONGTEXT NOT NULL', 'LONGTEXT NOT NULL'),
    ('definite_mails', 'LONGTEXT NOT NULL', 'LONGTEXT NOT NULL'),
    ('approval_timer', 'INT NOT NULL DEFAULT 0', 'INT NOT NULL DEFAULT 0'),
    ('resolution_timer', 'INT NOT NULL DEFAULT 0', 'INT NOT NULL DEFAULT 0'),
    ('escalation_required', 'TINYINT(1) NOT NULL DEFAULT 1', 'TINYINT(1) NOT NULL DEFAULT 1'),
    ('is_enabled', 'TINYINT(1) NOT NULL DEFAULT 1', 'TINYINT(1) NOT NULL DEFAULT 1'),
    ('created_at', 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP', 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'),
    ('updated_at', 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
     'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'),
)

# Expected DATA_TYPE per column when schema is complete.
_TARGET_TYPES = {
    'subject_category': ('varchar',),
    'device_type': ('varchar',),
    'device_friendly_name': ('varchar',),
    'device_ip': ('varchar',),
    'categories': ('longtext', 'mediumtext', 'text'),
    'escalation_mails': ('longtext', 'mediumtext', 'text'),
    'definite_mails': ('longtext', 'mediumtext', 'text'),
    'approval_timer': ('int', 'bigint', 'smallint', 'tinyint'),
    'resolution_timer': ('int', 'bigint', 'smallint', 'tinyint'),
    'escalation_required': ('tinyint', 'int', 'smallint', 'bigint'),
    'is_enabled': ('tinyint', 'int', 'smallint', 'bigint'),
    'created_at': ('datetime', 'timestamp'),
    'updated_at': ('datetime', 'timestamp'),
}


def _table_exists(cursor, table):
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        [table],
    )
    return cursor.fetchone()[0] > 0


def _column_exists(cursor, table, column):
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        [table, column],
    )
    return cursor.fetchone()[0] > 0


def _column_data_type(cursor, table, column):
    cursor.execute(
        """
        SELECT DATA_TYPE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        [table, column],
    )
    row = cursor.fetchone()
    return row[0].lower() if row else None


def is_target_policy_schema(cursor):
    """Return True when ``policy`` matches the production escalation schema."""
    if not _table_exists(cursor, 'policy'):
        return False
    for column, allowed_types in _TARGET_TYPES.items():
        if not _column_exists(cursor, 'policy', column):
            return False
        if _column_data_type(cursor, 'policy', column) not in allowed_types:
            return False
    return True


def _execute_alter(cursor, statement):
    logger.info("policy schema DDL: %s", statement)
    cursor.execute(statement)


def _normalize_text_timers_before_int_upgrade(cursor):
    """Convert empty text timer values before MODIFY to INT (legacy prod rows)."""
    for column in ('approval_timer', 'resolution_timer'):
        if not _column_exists(cursor, 'policy', column):
            continue
        if _column_data_type(cursor, 'policy', column) in _TARGET_TYPES[column]:
            continue
        if _column_data_type(cursor, 'policy', column) not in (
            'varchar', 'text', 'longtext', 'mediumtext', 'tinytext', 'char'
        ):
            continue
        _execute_alter(
            cursor,
            f"UPDATE policy SET {column} = '0' "
            f"WHERE {column} IS NULL OR TRIM({column}) = ''",
        )


def _upgrade_existing_policy_table(cursor):
    """Apply ADD/MODIFY only — preserves row data."""
    _normalize_text_timers_before_int_upgrade(cursor)
    alters = []
    for name, add_ddl, modify_ddl in _POLICY_COLUMNS:
        if not _column_exists(cursor, 'policy', name):
            alters.append(f'ADD COLUMN {name} {add_ddl}')
            continue
        current = _column_data_type(cursor, 'policy', name)
        allowed = _TARGET_TYPES.get(name, ())
        if current not in allowed:
            alters.append(f'MODIFY COLUMN {name} {modify_ddl}')

    if not alters:
        return []
    statement = f"ALTER TABLE policy {', '.join(alters)}"
    _execute_alter(cursor, statement)
    return alters


def upgrade_policy_schema(connection, dry_run=False):
    """
    Bring ``policy`` to the target schema. Returns a list of executed/skipped steps.
    Raises on SQL failure so ``migrate`` does not silently continue.
    """
    steps = []
    with connection.cursor() as cursor:
        if not _table_exists(cursor, 'policy'):
            steps.append(('create', 'CREATE TABLE policy'))
            if not dry_run:
                _execute_alter(cursor, POLICY_CREATE_SQL)
            return steps

        if is_target_policy_schema(cursor):
            steps.append(('skip', 'policy table already at target schema'))
            return steps

        if dry_run:
            pending = []
            for name, add_ddl, modify_ddl in _POLICY_COLUMNS:
                if not _column_exists(cursor, 'policy', name):
                    pending.append(f'ADD COLUMN {name} {add_ddl}')
                else:
                    current = _column_data_type(cursor, 'policy', name)
                    if current not in _TARGET_TYPES.get(name, ()):
                        pending.append(f'MODIFY COLUMN {name} {modify_ddl}')
            steps.append(('upgrade', pending or ['no changes']))
            return steps

        applied = _upgrade_existing_policy_table(cursor)
        if not is_target_policy_schema(cursor):
            raise RuntimeError(
                'policy schema upgrade incomplete after ALTER; '
                'check MySQL permissions and column types'
            )
        steps.append(('upgrade', applied or ['schema normalized']))
        return steps


def parse_policy_timer(value):
    """Coerce UI timer values to INT for the ``policy`` table."""
    if value is None or value == '':
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

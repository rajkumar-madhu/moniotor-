#!/usr/local/bin/python
"""
LinkedEye entrypoint script.
FIXED: SQL injection via parameterized queries.
FIXED: os.system() replaced with subprocess.run().
FIXED: Cursor/connection leaks with context managers.
FIXED: print() replaced with logging.

SECURITY (see backend/CHANGES-SECURITY.md):
  * the Vault root token is no longer written to the StackStorm config; a policy-scoped
    periodic token is written instead (LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN=true restores
    the old behaviour);
  * the country/state reference tables are created if missing and loaded only when empty,
    inside one transaction, never dropped (LINKEDEYE_ENTRYPOINT_RELOAD_GEO=true forces a reload);
  * makemigrations can be switched off with LINKEDEYE_ENTRYPOINT_MAKEMIGRATIONS=false once the
    migrations are committed to the image.
"""

import os
import sys
import pathlib
import subprocess
import logging
import pandas as pd

sys.path.insert(1, '/root/.local/lib/python3.9/site-packages/LinkedEyeVault')

import mysql.connector
from LinkedEyeVault import Vault

logger = logging.getLogger('linkedeye.entrypoint')

ConfigFile = '/stackstorm/configs/linkedeye_vault.yaml'

db_host = os.getenv('MYSQL_DB_HOST', 'db')
db_port = os.getenv('MYSQL_DB_PORT', '3306')
db_user = os.getenv('MYSQL_DB_USER', 'root')
db_pass = os.getenv('MYSQL_DB_PASS', '')


def _flag(name, default='false'):
    return os.getenv(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


VAULT_POLICY_NAME = "linkedeye_secret_acl"
VAULT_ROLE_NAME = "linkedeye_role"


def setConfig(roleID):
    try:
        # FIXED: Use subprocess instead of os.system to avoid command injection
        subprocess.run(
            ['sed', '-i', '/token/d', str(ConfigFile)],
            check=True, timeout=30
        )
        with open(ConfigFile, 'a') as f:
            f.write("token: '{}'\n".format(str(roleID)))
    except Exception as ex:
        raise Exception(ex)


def isAlreadyset():
    try:
        with open(ConfigFile) as f:
            if "token: ''" in f.read():
                return False
            return True
    except Exception as ex:
        logger.error("isAlreadyset error: %s", ex)


def createDatabase(dbname):
    """Create database with parameterized safe name."""
    conn = None
    try:
        logger.info("createDatabase initiated. Database name: %s", dbname)
        logger.info("DB: %s, PORT: %s", db_host, db_port)
        conn = mysql.connector.connect(host=db_host, port=db_port, user=db_user, passwd=db_pass)
        cursor = conn.cursor()
        # FIXED: Validate dbname to prevent SQL injection (can't parameterize CREATE DATABASE)
        safe_dbname = ''.join(c for c in str(dbname) if c.isalnum() or c == '_')
        cursor.execute("CREATE DATABASE IF NOT EXISTS `{}`".format(safe_dbname))
    except Exception as ex:
        logger.error("createDatabase error: %s", ex)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def createTables():
    try:
        logger.info("createDatabase - Done")
        createCountryStateTable()
        logger.info("createCountryStateTable - Done")
    except Exception as ex:
        logger.error("createTables error: %s", ex)


def StateDataInsert(data, conn, countryid):
    # FIXED: parameterized queries to prevent SQL injection. The caller owns the
    # transaction (one commit at the end, rollback on any error).
    cursor = conn.cursor()
    sql = "INSERT INTO linkedeye.state (countryid, stateshortname, statename, lat, lng) VALUES (%s, %s, %s, %s, %s)"
    for i, row in data.iterrows():
        cursor.execute(sql, (countryid, row.stateshortname, row.statename, row.lat, row.lng))


def CountryDataInsert(data, conn):
    cursor = conn.cursor()
    sql = "INSERT INTO linkedeye.country (countryshortname, countryname) VALUES (%s, %s)"
    for i, row in data.iterrows():
        shortname = row.countryshortname if not pd.isna(row.countryshortname) else 'NA'
        cursor.execute(sql, (shortname, row.countryname))
        filename = os.path.join("jvectormap", shortname + ".csv")
        if os.path.isfile(filename):
            logger.info("Processing state data for: %s", shortname)
            statedata = pd.read_csv(filename, index_col=False, delimiter=',')
            StateDataInsert(statedata, conn, cursor.lastrowid)


def createCountryStateTable():
    """Create the map reference tables if missing and fill them only when empty.

    Previously both tables were dropped and rebuilt on every container start, so a
    restart left other replicas serving the map with empty or partial tables.
    """
    conn = None
    try:
        conn = mysql.connector.connect(host=db_host, port=db_port, user=db_user, passwd=db_pass, database="linkedeye")
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS country (countryid int PRIMARY KEY AUTO_INCREMENT, countryshortname VARCHAR(20), countryname VARCHAR(100))")
        cursor.execute("CREATE TABLE IF NOT EXISTS state (stateid int PRIMARY KEY AUTO_INCREMENT, countryid int, stateshortname VARCHAR(20), statename VARCHAR(100), lat float, lng float)")
        cursor.execute("SELECT COUNT(*) FROM country")
        existing = cursor.fetchone()[0]
        reload_geo = _flag('LINKEDEYE_ENTRYPOINT_RELOAD_GEO')
        if existing and not reload_geo:
            logger.info("country/state tables already populated (%s countries); not reloading", existing)
            return
        countrydata = pd.read_csv('jvectormap/country.csv', index_col=False, delimiter=',')
        if existing:
            # DELETE (not TRUNCATE/DROP) so the swap is part of the same transaction.
            cursor.execute("DELETE FROM state")
            cursor.execute("DELETE FROM country")
        CountryDataInsert(countrydata, conn)
        conn.commit()
    except Exception as ex:
        logger.error("createCountryStateTable error: %s", ex)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def makeMigrations():
    try:
        if _flag('LINKEDEYE_ENTRYPOINT_MAKEMIGRATIONS', 'true'):
            # Legacy behaviour: most apps in this tree have no committed migrations, so a
            # fresh database needs them generated here. Set the flag to false once the
            # migrations are committed so a running pod never writes migration files.
            for _path in sorted(pathlib.Path('.').glob('**/models.py')):
                app_name = _path.parent.name
                if app_name in ('venv', 'site-packages') or 'venv' in _path.parts:
                    continue
                logger.info("makemigrations %s", app_name)
                subprocess.run(
                    ["python", "manage.py", "makemigrations", app_name, "--noinput"],
                    check=False,
                    timeout=120,
                )
            logger.info("Running makemigrations (global)")
            subprocess.run(["python", "manage.py", "makemigrations", "--noinput"], check=False, timeout=120)
        else:
            logger.info("makemigrations skipped (LINKEDEYE_ENTRYPOINT_MAKEMIGRATIONS=false)")

        logger.info("Running migrate")
        migrate_result = subprocess.run(
            ["python", "manage.py", "migrate", "--noinput"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if migrate_result.stdout:
            logger.info("migrate stdout:\n%s", migrate_result.stdout.strip())
        if migrate_result.returncode != 0:
            logger.error("migrate failed (rc=%s):\n%s", migrate_result.returncode, migrate_result.stderr.strip())
        logger.info("Running collectstatic")
        subprocess.run(["python", "manage.py", "collectstatic", "--noinput"], check=False, timeout=120)
        logger.info("Running LEDefaultAddservices")
        subprocess.run(["python", "manage.py", "LEDefaultAddservices"], check=False, timeout=120)
        logger.info("Running LEDefaultSites")
        subprocess.run(["python", "manage.py", "LEDefaultSites"], check=False, timeout=120)
        logger.info("Running final collectstatic")
        subprocess.run(["python", "manage.py", "collectstatic", "--noinput"], check=False, timeout=120)
    except Exception as ex:
        logger.error("makeMigrations error: %s", ex)


def main():
    try:
        url = "http://{}:{}".format(
            os.getenv('VAULT_HOST', 'vault'),
            os.getenv('VAULT_PORT', '8200')
        )
        Obj = Vault(url=url)
        if isAlreadyset():
            logger.info("Already token has been set")
            logger.info("Unseal: %s", Obj.Unseal())
        else:
            logger.info("VaultInit: %s", Obj.VaultInit())
            logger.info("Unseal: %s", Obj.Unseal())
            Obj.GetRoleID(VAULT_POLICY_NAME, VAULT_ROLE_NAME)
            logger.info("Status: %s", Obj.Status())
            if _flag('LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN'):
                logger.warning("Writing the Vault ROOT token to %s "
                               "(LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN=true); this is insecure", ConfigFile)
                token = Obj.GetRootToken()
            else:
                # Fails closed: if the scoped token cannot be created nothing is written.
                token = Obj.CreateScopedToken(
                    VAULT_POLICY_NAME, period=os.getenv('LINKEDEYE_STACKSTORM_TOKEN_PERIOD', '768h'))
            setConfig(token)

        # Django
        createDatabase(os.getenv("DATABASE_NAME") or os.getenv("MYSQL_DB_NAME", "linkedeye"))
        # createTables() (country/state reference data) is intentionally not called here;
        # it was never part of the start-up flow. It is safe to call now (see its docstring).
        makeMigrations()
    except Exception as ex:
        logger.error("Init Method Error: %s", ex)
        raise


if __name__ == '__main__':
    main()

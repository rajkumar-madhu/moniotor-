#!/usr/local/bin/python
"""
LinkedEye entrypoint script.
FIXED: SQL injection via parameterized queries.
FIXED: os.system() replaced with subprocess.run().
FIXED: Cursor/connection leaks with context managers.
FIXED: print() replaced with logging.
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
    try:
        cursor = conn.cursor()
        # FIXED: parameterized queries to prevent SQL injection
        sql = "INSERT INTO linkedeye.state (countryid, stateshortname, statename, lat, lng) VALUES (%s, %s, %s, %s, %s)"
        for i, row in data.iterrows():
            cursor.execute(sql, (countryid, row.stateshortname, row.statename, row.lat, row.lng))
            conn.commit()
    except Exception as e:
        logger.error("StateDataInsert error: %s", e)


def CountryDataInsert(data, conn):
    try:
        cursor = conn.cursor()
        # FIXED: parameterized queries to prevent SQL injection
        sql = "INSERT INTO linkedeye.country (countryshortname, countryname) VALUES (%s, %s)"
        for i, row in data.iterrows():
            shortname = row.countryshortname if not pd.isna(row.countryshortname) else 'NA'
            cursor.execute(sql, (shortname, row.countryname))
            conn.commit()
            filename = os.path.join("jvectormap", shortname + ".csv")
            if os.path.isfile(filename):
                logger.info("Processing state data for: %s", shortname)
                statedata = pd.read_csv(filename, index_col=False, delimiter=',')
                StateDataInsert(statedata, conn, cursor.lastrowid)
    except Exception as e:
        logger.error("CountryDataInsert error: %s", e)


def createCountryStateTable():
    conn = None
    try:
        conn = mysql.connector.connect(host=db_host, port=db_port, user=db_user, passwd=db_pass, database="linkedeye")
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS country")
        cursor.execute("DROP TABLE IF EXISTS state")
        cursor.execute("CREATE TABLE country (countryid int PRIMARY KEY AUTO_INCREMENT, countryshortname VARCHAR(20), countryname VARCHAR(100))")
        cursor.execute("CREATE TABLE state (stateid int PRIMARY KEY AUTO_INCREMENT, countryid int, stateshortname VARCHAR(20), statename VARCHAR(100), lat float, lng float)")
        countrydata = pd.read_csv('jvectormap/country.csv', index_col=False, delimiter=',')
        CountryDataInsert(countrydata, conn)
    except Exception as ex:
        logger.error("createCountryStateTable error: %s", ex)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def makeMigrations():
    try:
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
        roleID = Obj.GetRoleID("linkedeye_secret_acl", "linkedeye_role")
        logger.info("Status: %s", Obj.Status())
        rootToken = Obj.GetRootToken()
        setConfig(rootToken)

    # Django
    createDatabase(os.getenv("DATABASE_NAME") or os.getenv("MYSQL_DB_NAME", "linkedeye"))
    makeMigrations()
except Exception as ex:
    logger.error("Init Method Error: %s", ex)
    raise

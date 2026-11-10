"""backup.sh and restore.sh against a REAL PostgreSQL server (a private throw-away cluster, Unix socket only).

The stub `docker` below runs `docker compose exec -T db <command>` on this machine instead of in the db container, with
PGHOST pointing at the private cluster, so the exact pg_dump / pg_restore / psql pipelines of the scripts run for real.
Skipped when the PostgreSQL server binaries (initdb, pg_ctl) are not installed. As root the server runs as the `postgres`
user (PostgreSQL refuses to run as root); the client tools connect over the socket as the cluster's superuser `pg`.
"""
import glob
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_live import LiveCase, make_env

PASSTHROUGH_DOCKER = r'''#!/usr/bin/env bash
# docker stub: the stack is healthy, and "compose exec -T SERVICE CMD..." runs CMD here (against the private PostgreSQL).
printf 'docker %s\n' "$*" >> "$STUB_DIR/calls.log"
case "$1" in
  info) exit 0 ;;
  inspect) case "$3" in *ExitCode*) echo 0 ;; *State.Status*) echo running ;; *Health*) echo healthy ;; *) echo sha256:abc ;; esac; exit 0 ;;
  compose)
    shift
    while [ "$1" = -f ]; do shift 2; done
    case "$1" in
      ps) echo "cid-${*: -1}" ;;
      exec) shift; [ "$1" != -T ] || shift; shift; exec "$@" ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
'''

V1 = """
CREATE TABLE django_migrations (id serial PRIMARY KEY, app text NOT NULL, name text NOT NULL);
INSERT INTO django_migrations (app, name) VALUES ('core', '0001_initial');
CREATE TABLE core_site (id serial PRIMARY KEY, name text NOT NULL);
INSERT INTO core_site (name) VALUES ('old-site');
CREATE TABLE notes (body text);
INSERT INTO notes SELECT md5(g::text) FROM generate_series(1, 50000) g;
"""

# What a newer release does to the same database: a new table with a foreign key to an existing one (like hardware_* and
# hypervisor_* referencing network_device), its migration row, and new data.
UPGRADE = """
INSERT INTO core_site (name) VALUES ('new-site');
CREATE TABLE hardware_bmc (id serial PRIMARY KEY, site_id integer NOT NULL REFERENCES core_site (id));
INSERT INTO hardware_bmc (site_id) SELECT id FROM core_site WHERE name = 'new-site';
INSERT INTO django_migrations (app, name) VALUES ('hardware', '0001_initial');
"""


def _bindir():
    candidates = [os.environ.get("LE_PG_BINDIR", "")] + sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    which = shutil.which("initdb")
    if which:
        candidates.append(str(Path(which).parent))
    for c in candidates:
        if c and Path(c, "initdb").exists() and Path(c, "pg_ctl").exists():
            return Path(c)
    return None


class Cluster:
    def __init__(self):
        self.bindir = _bindir()
        if self.bindir is None:
            raise unittest.SkipTest("PostgreSQL server binaries (initdb, pg_ctl) are not installed")
        self.as_server = []
        # a short path: the socket path must stay under ~100 characters
        self.dir = Path(tempfile.mkdtemp(prefix="le-pg-", dir="/tmp" if Path("/tmp").is_dir() else None))
        if os.geteuid() == 0:
            try:
                user = pwd.getpwnam("postgres")
            except KeyError:
                shutil.rmtree(self.dir, ignore_errors=True)
                raise unittest.SkipTest("running as root and there is no postgres user to run the server as")
            runuser = shutil.which("runuser")
            if not runuser:
                shutil.rmtree(self.dir, ignore_errors=True)
                raise unittest.SkipTest("running as root and runuser is missing")
            os.chown(self.dir, user.pw_uid, user.pw_gid)
            self.as_server = [runuser, "-u", "postgres", "--"]
        os.chmod(self.dir, 0o755)
        self.data = self.dir / "data"
        self._server("initdb", "-D", str(self.data), "-U", "pg", "-A", "trust", "--no-sync")
        self._server("pg_ctl", "-D", str(self.data), "-l", str(self.dir / "server.log"), "-w", "-o",
                     f"-p 5432 -k {self.dir} -c listen_addresses='' -c fsync=off", "start")
        self.env = {"PGHOST": str(self.dir), "PGPORT": "5432", "PGUSER": "pg"}

    def _server(self, tool, *args):
        subprocess.run(self.as_server + [str(self.bindir / tool), *args], check=True, capture_output=True, timeout=120)

    def stop(self):
        subprocess.run(self.as_server + [str(self.bindir / "pg_ctl"), "-D", str(self.data), "-m", "immediate", "stop"],
                       capture_output=True, timeout=60)
        shutil.rmtree(self.dir, ignore_errors=True)

    def psql(self, db, sql):
        proc = subprocess.run([str(self.bindir / "psql"), "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", sql],
                              capture_output=True, text=True, env={**os.environ, **self.env}, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        return proc.stdout.strip()


class RestoreAgainstPostgresTests(LiveCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = Cluster()

    @classmethod
    def tearDownClass(cls):
        cls.pg.stop()

    def setUp(self):
        super().setUp()
        self.db = f"le_{self._testMethodName[-40:]}".lower()
        self.pg.psql("postgres", f"DROP DATABASE IF EXISTS {self.db}")
        self.pg.psql("postgres", f"CREATE DATABASE {self.db}")
        bin_dir = self.stub / "passthrough-bin"          # not stub/bin: make_env writes test_live's stub docker there
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(PASSTHROUGH_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        # http mode on the discard port: the closing health check fails fast (restore.sh only warns about it)
        self.env_file.write_text("LINKEDEYE_DOMAIN=localhost\nLINKEDEYE_MODE=http\nLINKEDEYE_HTTP_PORT=9\n"
                                 "LINKEDEYE_CREDENTIAL_KEY=k\nLINKEDEYE_SECRET_KEY=s\nLINKEDEYE_DB_PASSWORD=p\n")

    def script_env(self):
        return {**self.pg.env, "POSTGRES_USER": "pg", "POSTGRES_DB": self.db,
                "PATH": f"{self.stub / 'passthrough-bin'}:{self.pg.bindir}:{os.environ['PATH']}"}

    def run_live(self, name, *args):
        proc = subprocess.run([str(self.live / name), *args], capture_output=True, text=True, timeout=300, cwd=self.tmp,
                              env=make_env(self.stub, self.script_env()))
        return proc

    def backup(self):
        proc = self.run_live("backup.sh", "--dir", str(self.tmp / "backups"), "--quiet")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return Path(proc.stdout.strip())

    def state(self):
        return {
            "sites": self.pg.psql(self.db, "SELECT string_agg(name, ',' ORDER BY id) FROM core_site"),
            "migrations": self.pg.psql(self.db, "SELECT string_agg(app || '.' || name, ',' ORDER BY id) FROM django_migrations"),
            "hardware_bmc": self.pg.psql(self.db, "SELECT coalesce(to_regclass('hardware_bmc')::text, '')"),
            "notes": self.pg.psql(self.db, "SELECT count(*) FROM notes"),
        }

    def test_an_older_dump_restores_onto_a_newer_schema(self):
        # update.sh's rollback: old code + the pre-update backup, after the new release added a table with a foreign key
        self.pg.psql(self.db, V1)
        archive = self.backup()
        self.pg.psql(self.db, UPGRADE)
        proc = self.run_live("restore.sh", str(archive), "--yes", "--no-safety-backup")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.state(), {"sites": "old-site", "migrations": "core.0001_initial", "hardware_bmc": "", "notes": "50000"})

    def test_a_restore_that_fails_half_way_changes_nothing(self):
        self.pg.psql(self.db, V1)
        archive = self.backup()
        self.pg.psql(self.db, UPGRADE)
        before = self.state()
        self.assertEqual(before["hardware_bmc"], "hardware_bmc")
        work = self.tmp / "work"
        work.mkdir()
        subprocess.run(["tar", "-C", str(work), "-xf", str(archive)], check=True)
        dump = (work / "db.dump").read_bytes()
        for label, damaged in (("cut in the middle of the table data", dump[: len(dump) * 2 // 3]),
                               ("not a dump at all", b"PGDMP\xff\xff not a custom-format archive")):
            with self.subTest(label):
                (work / "db.dump").write_bytes(damaged)
                # valid checksums: the archive check passes, pg_restore is what fails
                subprocess.run(["sh", "-c", "sha256sum db.dump env MANIFEST > SHA256SUMS"], cwd=work, check=True)
                broken = self.tmp / "broken.tar"
                subprocess.run(["tar", "-C", str(work), "-cf", str(broken), "db.dump", "env", "MANIFEST", "SHA256SUMS"], check=True)
                proc = self.run_live("restore.sh", str(broken), "--yes", "--no-safety-backup")
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("rolled back", proc.stderr)
                self.assertEqual(self.state(), before)          # nothing dropped, nothing half-restored


if __name__ == "__main__":
    unittest.main()

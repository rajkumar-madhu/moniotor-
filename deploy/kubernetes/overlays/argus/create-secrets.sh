#!/usr/bin/env bash
# Creates Secret "linkedeye-secrets" in namespace fs-linkedeye with freshly generated keys (nothing is printed or
# written to disk except into the cluster). Run once from a machine whose kubectl points at the right cluster.
#
#   DB_PASSWORD='<password>' ./create-secrets.sh          # your own PostgreSQL
#   ./create-secrets.sh                                    # generates a DB password (use with optional/postgres.yaml)
#
# Re-running refuses to overwrite an existing Secret (rotating keys needs thought: see docs/ARGUS_K8S.md).
set -euo pipefail
NS="${NAMESPACE:-fs-linkedeye}"
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
if kubectl -n "$NS" get secret linkedeye-secrets >/dev/null 2>&1; then
  echo "Secret linkedeye-secrets already exists in $NS; not touching it." >&2
  exit 0
fi
gen() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
fernet() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }
DB_PASSWORD="${DB_PASSWORD:-$(gen 24)}"
case "$DB_PASSWORD" in *$'\n'*|*$'\r'*) echo "DB_PASSWORD must be a single line" >&2; exit 1 ;; esac
# The values reach kubectl on its standard input (printf is a shell builtin), never on a command line: another user of the
# bastion could read kubectl's arguments from ps or /proc while it runs, and audit tools log them.
{
  printf 'LINKEDEYE_SECRET_KEY=%s\n' "$(gen 50)"
  printf 'LINKEDEYE_DB_PASSWORD=%s\n' "$DB_PASSWORD"
  printf 'LINKEDEYE_ALERTMANAGER_WEBHOOK_TOKEN=%s\n' "$(gen 32)"
  printf 'LINKEDEYE_CREDENTIAL_KEY=%s\n' "$(fernet)"
} | kubectl -n "$NS" create secret generic linkedeye-secrets --from-env-file=/dev/stdin >/dev/null
echo "Created linkedeye-secrets in $NS (SECRET_KEY, DB_PASSWORD, ALERTMANAGER_WEBHOOK_TOKEN, CREDENTIAL_KEY)."
echo "Back up LINKEDEYE_CREDENTIAL_KEY: without it stored server/BMC/vCenter passwords cannot be decrypted:"
echo "  kubectl -n $NS get secret linkedeye-secrets -o jsonpath='{.data.LINKEDEYE_CREDENTIAL_KEY}' | base64 -d"

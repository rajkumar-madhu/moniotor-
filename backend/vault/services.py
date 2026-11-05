"""
Vault service layer (PREVENTIVE FIX #27).

Encapsulates business logic for HashiCorp Vault operations: seal/unseal
status, secret CRUD, and secret listing.  Functions accept plain Python data
and return dicts -- never Django request/response objects.
"""

import json
import logging

from lib.LinkedEyeVault import Vault
from vault.models import VaultModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vault status
# ---------------------------------------------------------------------------

def get_vault_status():
    """Return the current seal status of the Vault instance.

    Returns:
        dict: ``{'status': int, 'seal_status': str}`` where ``seal_status``
        is ``"Sealed"`` or ``"Unsealed"``.
    """
    try:
        temp_vault = Vault()
        res = temp_vault.Status()
        if res['status'] == 200:
            data = res['data']
            if data['status'] == 200:
                msg = json.loads(data['msg'])
                seal_status = "Sealed" if msg['sealed'] else "Unsealed"
            else:
                seal_status = "Unsealed"
        else:
            seal_status = "Unsealed"
        return {'status': 200, 'seal_status': seal_status}
    except Exception as e:
        logger.exception("get_vault_status failed")
        return {'status': 500, 'seal_status': 'Unknown', 'msg': str(e)}


# ---------------------------------------------------------------------------
# Secret CRUD
# ---------------------------------------------------------------------------

def vault_add_secret(service, ip, username, password):
    """Add a secret to Vault and persist its metadata in the local DB.

    Args:
        service (str): Service name (e.g. ``"Gateway"``).
        ip (str): Server IP address.
        username (str): Credential username.
        password (str): Credential password.

    Returns:
        dict: ``{'status': int, 'rowid': int, 'url': str}`` on success,
        or ``{'status': int, 'msg': str}`` on failure.
    """
    response = {}
    try:
        temp_vault = Vault()
        url = f'/secret/{service}/{ip}/{username}'
        res = temp_vault.addSecret(service, ip, username, password)

        if res['status'] == 200:
            data = res['data']
            if data['status'] == 204:
                if not VaultModel.objects.filter(url=url).exists():
                    ob_secret = VaultModel(service=service, server=ip, user=username, url=url)
                    ob_secret.save()
                    obj = VaultModel.objects.get(url=url)
                    response['rowid'] = obj.id
                    response['url'] = url
                    response['status'] = data['status']
                else:
                    response['status'] = 200
                    response['msg'] = 'Secret URL already exists in local DB'
            else:
                response['status'] = data.get('status', 400)
                response['msg'] = 'Vault did not return 204'
        else:
            response['status'] = res['status']
            response['msg'] = 'Vault API error'
    except Exception as e:
        logger.exception("vault_add_secret failed for %s/%s/%s", service, ip, username)
        response['status'] = 400
        response['msg'] = str(e)
    return response


def vault_delete_secret(service, ip, username):
    """Delete a secret from Vault and remove its local DB record.

    Returns:
        dict: ``{'status': int, 'rowid': int}`` on success.
    """
    response = {}
    try:
        temp_vault = Vault()
        url = f'/secret/{service}/{ip}/{username}'
        res = temp_vault.delSecret(service, ip, username)

        if res['status'] == 200:
            data = res['data']
            if data['status'] == 204:
                db_obj = VaultModel.objects.get(url=url)
                response['status'] = data['status']
                response['rowid'] = db_obj.id
                db_obj.delete()
            else:
                response['status'] = data.get('status', 400)
                response['msg'] = 'Vault did not return 204'
        else:
            response['status'] = res['status']
            response['msg'] = 'Vault API error'
    except Exception as e:
        logger.exception("vault_delete_secret failed for %s/%s/%s", service, ip, username)
        response['status'] = 400
        response['msg'] = str(e)
    return response


def vault_update_secret(service, ip, username, password, row_id=None):
    """Update a secret in Vault and refresh local DB metadata.

    Args:
        service (str): Service name.
        ip (str): Server IP.
        username (str): Credential username.
        password (str): New password.
        row_id (int|None): Existing VaultModel row ID.

    Returns:
        dict: ``{'status': int, 'url': str, 'rowid': int}``
    """
    response = {}
    try:
        temp_vault = Vault()
        url = f'/secret/{service}/{ip}/{username}'
        res = temp_vault.updateSecret(service, ip, username, password)

        if res['status'] == 200:
            data = res['data']
            if data['status'] == 204:
                if row_id:
                    obj = VaultModel.objects.get(id=row_id)
                    obj.server = ip
                    obj.url = url
                    obj.save()
                response['url'] = url
                response['rowid'] = row_id
                response['status'] = data['status']
            else:
                response['status'] = data.get('status', 400)
                response['msg'] = 'Vault did not return 204'
        else:
            response['status'] = res['status']
            response['msg'] = 'Vault API error'
    except Exception as e:
        logger.exception("vault_update_secret failed for %s/%s/%s", service, ip, username)
        response['status'] = 400
        response['msg'] = str(e)
    return response


# ---------------------------------------------------------------------------
# Seal / Unseal
# ---------------------------------------------------------------------------

def vault_seal():
    """Seal the Vault instance.

    Returns:
        dict: ``{'status': str|int, 'code': int, 'msg': str}``
    """
    try:
        temp_vault = Vault()
        res = temp_vault.Seal()
        if res['status'] == 200:
            return {'status': res['data']}
        else:
            return {'status': 'Unsealed', 'code': res['status'], 'msg': 'Not able to Seal'}
    except Exception as e:
        logger.exception("vault_seal failed")
        return {'status': 400, 'code': 400, 'msg': 'Something went wrong'}


def vault_unseal():
    """Unseal the Vault instance.

    Returns:
        dict: ``{'status': str|int, 'code': int, 'msg': str}``
    """
    try:
        temp_vault = Vault()
        res = temp_vault.Unseal()
        if res['status'] == 200:
            return {'status': res['data']}
        else:
            return {'status': 'Sealed', 'code': res['status'], 'msg': 'Not able to Unseal'}
    except Exception as e:
        logger.exception("vault_unseal failed")
        return {'status': 400, 'code': 400, 'msg': 'Something went wrong'}


# ---------------------------------------------------------------------------
# Secret listing
# ---------------------------------------------------------------------------

def get_all_secrets():
    """Retrieve all secrets metadata from the local DB plus Vault seal status.

    Returns:
        dict: ``{'status': int, 'data': list[dict], 'secretstatus': str}``
    """
    response = {}
    try:
        vault_info = get_vault_status()
        status = vault_info.get('seal_status', 'Unknown')

        temp_list = []
        for secret in VaultModel.objects.all():
            temp_list.append({
                "id": secret.id,
                "username": secret.user,
                "service": secret.service,
                "ip": secret.server,
                "url": secret.url,
            })
        response['status'] = 200
        response['data'] = temp_list
        response['secretstatus'] = status
    except Exception as e:
        logger.exception("get_all_secrets failed")
        response['status'] = 400
        response['msg'] = 'Something went wrong'
    return response

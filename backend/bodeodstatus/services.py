"""
BOD/EOD status service layer (PREVENTIVE FIX #27).

Encapsulates business logic for fetching and updating Redis BOD/EOD keys.
Functions accept plain Python data and return dicts -- never Django
request/response objects.
"""

import ast
import logging

from lib.LinkedEyeRedis import Redis
from lesites.models import SiteModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis key operations
# ---------------------------------------------------------------------------

def get_bod_eod_keys(site_config, mode="ALL", ip=""):
    """Fetch and parse BOD/EOD keys from Redis for a single site.

    Args:
        site_config (dict): Must contain ``redis_host``, ``redis_port``,
            and ``sitename``.
        mode (str): Query mode -- ``"ALL"``, ``"VER"``, etc.
        ip (str): IP filter (used when *mode* is ``"VER"``).

    Returns:
        dict: ``{'site': str, 'site_data': list[dict], 'code': int}``
        where each element of ``site_data`` has ``"key"`` and ``"key_data"``.
    """
    temp_obj = {'site': site_config['sitename']}
    try:
        redisObj = Redis(
            host=site_config['redis_host'],
            port=site_config['redis_port'],
        )

        if mode != "VER":
            redisKeys = redisObj.getBodEodkeys(site=site_config['sitename'], mode=mode)
        else:
            redisKeys = redisObj.getBodEodkeys(
                site=site_config['sitename'], mode=mode, ip=ip,
            )

        keys = []
        for key in redisKeys:
            entry = {"key": key}
            try:
                raw_value = redisObj.get(key)
                entry["key_data"] = ast.literal_eval(raw_value)
            except Exception as e:
                logger.warning("Failed to parse Redis key=%s: %s", key, e)
                entry["key_data"] = None
            keys.append(entry)

        temp_obj["site_data"] = keys
        temp_obj["code"] = 200
    except Exception as e:
        logger.exception(
            "get_bod_eod_keys failed for site=%s", site_config.get('sitename'),
        )
        temp_obj["site_data"] = []
        temp_obj["code"] = 500
    return temp_obj


def get_bod_eod_keys_for_site(sitename, mode="ALL", ip=""):
    """Convenience wrapper: look up the site by name and fetch its keys.

    Args:
        sitename (str): Site name (from ``SiteModel.sitename``).
        mode (str): Query mode.
        ip (str): IP filter.

    Returns:
        dict: ``{'status': int, 'refreshedsite': str, 'responseData': list}``
    """
    response = {"refreshedsite": sitename}
    response_data = []

    site_objs = SiteModel.objects.filter(sitename=sitename)
    for site_obj in site_objs:
        cfg = {
            'redis_host': site_obj.redis_host,
            'redis_port': site_obj.redis_port,
            'sitename': site_obj.sitename,
        }
        response_data.append(get_bod_eod_keys(cfg, mode, ip))

    response["status"] = 200
    response["responseData"] = response_data
    return response


def update_bod_eod_key(sitename, existing_key, value):
    """Update a single Redis BOD/EOD key.

    Args:
        sitename (str): Site name for Redis connection lookup.
        existing_key (str): The Redis key to update.
        value (str): New value for the key.

    Returns:
        dict: ``{'status': int, 'refreshedsite': str,
        'responseData': dict}``
    """
    response = {"refreshedsite": sitename}
    try:
        site_objs = SiteModel.objects.filter(sitename=sitename)
        temp_obj = {}

        for site_obj in site_objs:
            temp_obj['site'] = site_obj.sitename
            try:
                redisObj = Redis(
                    host=site_obj.redis_host,
                    port=site_obj.redis_port,
                )
                redisResult = redisObj.update(
                    existing_key=existing_key,
                    add_key='edit',
                    add_value=value,
                )
                if redisResult.get('status') == 200:
                    temp_obj["site_data"] = f'{existing_key} has been updated.'
                    temp_obj["code"] = 200
                else:
                    temp_obj["site_data"] = f'{existing_key} is not updated.'
                    temp_obj["code"] = 500
            except Exception as e:
                logger.exception(
                    "Redis update failed for key=%s site=%s",
                    existing_key, sitename,
                )
                temp_obj["site_data"] = []
                temp_obj["code"] = 500

        response["status"] = 200
        response["responseData"] = temp_obj
    except Exception as e:
        logger.exception("update_bod_eod_key failed for site=%s key=%s", sitename, existing_key)
        response['status'] = 400
        response['responseData'] = 'Something went wrong while updating EDIT keys'
    return response

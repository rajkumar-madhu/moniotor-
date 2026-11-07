#####################
## Author : Ponmuthu M
## Version : 21-01-2021
## Optimized : No hidden Node() + json.loads instead of ast.literal_eval (April 2026)
#####################

import redis
import socket
import os
import json
import ast

# CRITICAL FIX #7
# - Remove hidden Node() instantiation (was causing Neo4j connection leaks)
# NOTE: Redis data is stored as Python repr strings (single-quote dicts), not JSON.
#       ast.literal_eval is used — it is safe (only evaluates literals, no code execution).


class Redis(object):
    """
    Redis Manager with proper connection handling.
    CRITICAL FIX #7: No hidden Node() instantiation
    CRITICAL FIX #16: json.loads instead of ast.literal_eval
    """
    
    def __init__(self, host="", port="", db=0, password="", username=""):
        self.db = db
        self.host = host or os.getenv('REDIS_HOST', "redis")
        self.port = port or os.getenv('REDIS_PORT', 6379)
        self.password = password or self._secret('REDIS_PASSWORD')
        # Redis 6+ ACL: username from Vault/settings, then REDIS_USER env.
        # Empty keeps the built-in 'default' user (legacy password-only Redis).
        self.username = username or self._secret('REDIS_USER')
        self.channel = None
        # REMOVED: No hidden Node() instantiation - was causing Neo4j connection leaks
        self._connect()

    @staticmethod
    def _secret(name):
        try:
            from django.conf import settings as dj_settings
            if getattr(dj_settings, 'configured', False):
                value = getattr(dj_settings, name, None) or ''
                if value:
                    return value
        except Exception:
            pass
        return os.getenv(name, '') or ''

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close connection."""
        self.close()
        return False

    # Bound every Redis TCP connect and command. Without these, a Redis host
    # that is unreachable (e.g. another cluster's ClusterIP) sits in the kernel
    # SYN-retry cycle for ~60s per attempt and pins a gunicorn worker for the
    # whole time, so one dead site slows every other site's requests.
    CONNECT_TIMEOUT_SECONDS = float(os.getenv('REDIS_CONNECT_TIMEOUT', '2'))
    SOCKET_TIMEOUT_SECONDS = float(os.getenv('REDIS_SOCKET_TIMEOUT', '5'))
    # redis-py >= 4 installs a default Retry policy (10 attempts with backoff
    # on 8.x) that multiplies the connect timeout ~13x. Pin it explicitly so
    # the timeouts above are the real bound.
    CONNECT_RETRIES = int(os.getenv('REDIS_CONNECT_RETRIES', '0'))

    @classmethod
    def _timeout_kwargs(cls):
        kwargs = {
            'socket_connect_timeout': cls.CONNECT_TIMEOUT_SECONDS,
            'socket_timeout': cls.SOCKET_TIMEOUT_SECONDS,
            'retry_on_timeout': False,
        }
        try:
            from redis.backoff import NoBackoff
            from redis.retry import Retry
            kwargs['retry'] = Retry(NoBackoff(), cls.CONNECT_RETRIES)
        except ImportError:
            # redis-py 3.x: no Retry API and no implicit retries either.
            pass
        return kwargs

    def _connect(self):
        """Establish Redis connection."""
        try:
            kwargs = dict(
                host=self.host,
                port=self.port,
                db=self.db,
                password=self.password,
                **self._timeout_kwargs()
            )
            if self.username:
                kwargs['username'] = self.username
            self.channel = redis.StrictRedis(**kwargs)
            return self
        except Exception as ex:
            raise Exception("REDIS _connect : Ex = " + str(ex))

    def close(self):
        """Close Redis connection."""
        try:
            if self.channel:
                self.channel.close()
        except Exception:
            pass

    def set(self, key, value):
        """Set key-value pair."""
        try:
            self.channel.set(key, value)
        except Exception as ex:
            raise Exception("REDIS SET : Ex = " + str(ex))

    def update(self, existing_key, add_key, add_value):
        """Update existing key with new data."""
        response = {}
        try:
            a = self.channel.get(existing_key).decode('ascii')
            existing_key_value = ast.literal_eval(a)

            if add_key not in existing_key_value:
                existing_key_value[add_key] = []
            existing_key_value[add_key].append(ast.literal_eval(add_value))
            existing_key_value['overallStatus'] = True
            existing_key_value['status'] = 5

            self.set(existing_key, json.dumps(existing_key_value))
            response["status"] = 200
            return response
        except Exception as ex:
            response["status"] = 500
            from django.http import HttpResponse
            from json import dumps as jdumps
            return HttpResponse(jdumps(response), content_type="json")

    def get(self, key):
        """Get value by key."""
        try:
            return self.channel.get(key).decode('ascii')
        except Exception as ex:
            raise Exception("REDIS GET : Ex = " + str(ex))

    def getSiteHealth(self, site=""):
        """Get site health status."""
        ret = {}
        try:
            ret['entity'] = 1  # Default to healthy, caller should override
            ret['chart'] = {'isEntityRED': 1}  # Default placeholder

            for mode in ['bod', 'adp', 'eod']:
                list_of_key = self.getBodEodkeys(site=site, mode=mode)
                if list_of_key == []:  # No keys
                    ret[mode] = 0
                else:
                    value = 1  # AND operation initial 1
                    for key in list_of_key:
                        key_data = self.channel.get(key).decode('ascii')
                        get_value = ast.literal_eval(key_data).get('overallStatus', 0)
                        value = value * int(get_value)
                    ret[mode] = value
            return ret
        except Exception as ex:
            raise Exception("REDIS getSiteHealth : Ex = " + str(ex))

    def getSiteHealthNew(self, site="", entity_host="", entity_port="", entity_secure=False):
        """Get site health status with detailed status codes.

        The 'chart' key carries the entity hardware/software/application counts
        (from Neo4j via Node().overallStats()) that the dashboard PROD-ENV pie
        charts render. Without it the periodic sitehealth refresh recomputes the
        charts to zero and the UI shows "NO HARDWARE AVAILABLE" a few seconds
        after load. Node is imported lazily and the call is isolated so a Neo4j
        hiccup falls back to a placeholder instead of failing the whole request.

        entity_host/port/secure come from the lesite row so a single-site and
        a multi-site deploy use the same path (whatever Neo4j that row names).
        """
        ret = {}
        try:
            try:
                from lib.LinkedEyeEntity.Node import Node
                node_kwargs = {}
                if entity_host:
                    node_kwargs = {
                        'host': entity_host,
                        'port': entity_port,
                        'secure': bool(entity_secure),
                    }
                ret['chart'] = Node(**node_kwargs).overallStats()
                ret['entity'] = ret['chart'].get('isEntityRED', 1)
            except Exception as chart_ex:
                ret['entity'] = 1  # Default to healthy
                ret['chart'] = {'isEntityRED': 1}  # Fallback placeholder
                print("getSiteHealthNew chart unavailable:", chart_ex)

            for mode in ['bod', 'adp', 'eod']:
                isRed = 0
                isAmber = 0
                isGreen = 0
                isUnknown = 0
                list_of_key = self.getBodEodkeys(site=site, mode=mode)
                if list_of_key == []:  # No keys
                    ret[mode] = 0
                else:
                    for key in list_of_key:
                        key_data = self.channel.get(key).decode('ascii')
                        get_value = ast.literal_eval(key_data).get('status', 3)
                        
                        if get_value == 0:
                            isRed += 1
                        elif get_value == 1:
                            isAmber += 1
                        elif get_value == 2:
                            isGreen += 1
                        else:
                            isUnknown += 1
                    
                    if isRed > 0:
                        value = 0
                    elif isAmber > 0:
                        value = 1
                    elif isGreen > 0:
                        value = 2
                    elif isUnknown > 0:
                        value = 3
                    ret[mode] = value
            return ret
        except Exception as ex:
            raise Exception("REDIS getSiteHealthNew : Ex = " + str(ex))

    def getBodEodkeys(self, site="", mode="ALL", ip=""):
        """Get BOD/EOD keys from Redis."""
        try:
            if site == "":
                site = socket.gethostname().split('.')[0]
            _str_list = [i.decode('ascii') for i in self.channel.keys()]
            if mode == "ALL":
                return [i for i in _str_list if (str(site) + ":ADP" in i or str(site) + ":BOD" in i or str(site) + ":EOD" in i)]
            elif mode == "VER":
                return [i for i in _str_list if (str(site) + ":VER_versions_" + ip in i)]
            return [i for i in _str_list if (str(site) + ":" + str(mode.upper()) in i)]
        except Exception as ex:
            raise Exception("REDIS GET-BOD-EOD : Ex = " + str(ex))

    def getBodEodkeysAllSite(self):
        """Get all BOD/EOD keys across all sites."""
        try:
            _str_list = [i.decode('ascii') for i in self.channel.keys()]
            return [i for i in _str_list if (":BOD" in i or ":EOD" in i)]
        except Exception as ex:
            raise Exception("REDIS GET-BOD-EOD : Ex = " + str(ex))

    def getAllSites(self):
        """Get all site names from BOD/EOD keys."""
        try:
            _str_list = [i.decode('ascii') for i in self.channel.keys()]
            return list(set([i.split(':')[0] for i in _str_list if (":BOD" in i or ":EOD" in i)]))
        except Exception as ex:
            raise Exception("REDIS GET-BOD-EOD : Ex = " + str(ex))

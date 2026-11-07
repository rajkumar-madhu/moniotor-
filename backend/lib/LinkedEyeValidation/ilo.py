"""
iLO (HP/HPE Integrated Lights-Out) health check via Redfish API.
FIXED: Added proper logout to prevent TCP connection leak.
"""

import redfish
import logging

logger = logging.getLogger('linkedeye')


class ilo():
    def __init__(self, ip=None, username=None, password=None):
        self.ip = ip
        self.url = "https://{}".format(self.ip)
        self.username = username
        self.password = password

    def check(self):
        REST_OBJ = None
        try:
            REST_OBJ = redfish.redfish_client(
                base_url=self.url,
                username=self.username,
                password=self.password,
                timeout=2,
                max_retry=1,
            )
            REST_OBJ.login(auth="session")
            response = REST_OBJ.get('/redfish/v1/systems/1')
            return '20' in str(response.status)
        except Exception as ex:
            logger.debug("iLO check failed for %s: %s", self.ip, ex)
            return False
        finally:
            # FIXED: Always logout to release the session/TCP connection
            if REST_OBJ is not None:
                try:
                    REST_OBJ.logout()
                except Exception:
                    pass

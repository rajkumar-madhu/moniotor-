"""
SNMP validation check.
FIXED: Command injection via shlex.quote(), subprocess instead of os.system().
"""

import subprocess
import shlex
import logging

logger = logging.getLogger('linkedeye')


class snmp():
    def __init__(self, version=None, community_string=None, securitylevel=None,
                 username=None, authenticationmethod=None, authenticationpassword=None,
                 privacymethod=None, privacypassword=None, ip=None):
        self.version = version
        self.community_string = community_string
        self.securitylevel = securitylevel
        self.username = username
        self.authenticationmethod = authenticationmethod
        self.authenticationpassword = authenticationpassword
        self.privacymethod = privacymethod
        self.privacypassword = privacypassword
        self.ip = ip

    def check(self):
        """Execute snmpwalk with safely escaped parameters."""
        try:
            cmd_parts = ['snmpwalk']

            if self.version == 'v2c':
                cmd_parts.extend([
                    '-' + self.version,
                    '-c', self.community_string,
                    self.ip,
                    '1.3.6.1.2.1.2.2.1.8'
                ])
            elif self.version == 'v3' and self.securitylevel == 'authPriv':
                cmd_parts.extend([
                    '-' + self.version,
                    '-l', self.securitylevel,
                    '-u', self.username,
                    '-a', self.authenticationmethod,
                    '-A', self.authenticationpassword,
                    '-x', self.privacymethod,
                    '-X', self.privacypassword,
                    self.ip,
                    '1.3.6.1.2.1.2.2.1.8'
                ])
            elif self.version == 'v3' and self.securitylevel == 'authNoPriv':
                cmd_parts.extend([
                    '-' + self.version,
                    '-l', self.securitylevel,
                    '-u', self.username,
                    '-a', self.authenticationmethod,
                    '-A', self.authenticationpassword,
                    self.ip,
                    '1.3.6.1.2.1.2.2.1.8'
                ])
            elif self.version == 'v3' and self.securitylevel == 'noAuthNoPriv':
                cmd_parts.extend([
                    '-' + self.version,
                    '-l', self.securitylevel,
                    '-u', self.username,
                    self.ip,
                    '1.3.6.1.2.1.2.2.1.8'
                ])
            else:
                logger.error("SNMP check: unsupported version/securitylevel: %s/%s", self.version, self.securitylevel)
                return False

            result = subprocess.run(
                cmd_parts,
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning("SNMP check timed out for %s", self.ip)
            return False
        except Exception as ex:
            logger.error("SNMP check failed for %s: %s", self.ip, ex)
            return False

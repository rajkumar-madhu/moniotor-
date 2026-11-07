import requests
import json
import os

class Vault(object):
    def __init__(self, url="", keyfile="/vault/keys.json"):
        self.response = {'status' : 400 , 'data' : "" , "error_msg" : ""}
        self.keyfile = keyfile
        if url:
            self.url = url
        else:
            from django.conf import settings
            self.url = settings.VAULT_URL
        self.keys = {}
        self.initData = {"secret_shares": 1, "secret_threshold": 1}
        self.aclPolicy = {
        "policy": "# Manage auth methods broadly across Vault\npath \"auth/*\"\n{\n  capabilities = [\"create\", \"read\", "
            "\"update\", \"delete\", \"list\", \"sudo\"]\n}\n\n# Create, update, and delete auth methods\npath "
            "\"sys/auth/*\"\n{\n  capabilities = [\"create\", \"update\", \"delete\", \"sudo\"]\n}\n\n# List auth "
            "methods\npath \"sys/auth\"\n{\n  capabilities = [\"read\"]\n}\n\n# List existing policies\npath "
            "\"sys/policies/acl\"\n{\n  capabilities = [\"list\"]\n}\n\n# Create and manage ACL policies \npath "
            "\"sys/policies/acl/*\"\n{\n  capabilities = [\"create\", \"read\", \"update\", \"delete\", \"list\", "
            "\"sudo\"]\n}\n\n# List, create, update, and delete key/value secrets\npath \"secret/*\"\n{\n  "
            "capabilities = [\"create\", \"read\", \"update\", \"delete\", \"list\", \"sudo\"]\n}\n\n# Manage secrets "
            "engines\npath \"sys/mounts/*\"\n{\n  capabilities = [\"create\", \"read\", \"update\", \"delete\", "
            "\"list\", \"sudo\"]\n}\n\n# List existing secrets engines.\npath \"sys/mounts\"\n{\n  capabilities = ["
            "\"read\"]\n}\n\n# Read health checks\npath \"sys/health\"\n{\n  capabilities = [\"read\", \"sudo\"]\n} "
        }
        self.unsealData = {}
        self.isUnsealed = ""
        self._SealCheck()
        self._retrieve_keys()

    def _store_keys(self, key):
        """CRITICAL FIX #20: Use context manager for file handles."""
        with open(self.keyfile, 'w') as f:
            f.write(json.dumps(key))

    def _retrieve_keys(self):
        """CRITICAL FIX #16: json.loads instead of ast.literal_eval."""
        if self.keys == {}:
            if not os.stat(self.keyfile).st_size == 0:
                with open(self.keyfile) as f:
                    # CRITICAL FIX #16: json.loads instead of ast.literal_eval
                    self.keys = json.loads(f.read())

    def _postman(self, url, method="put", data={}):
        try:
            ret = {}
            headers = {}
            if self.keys:
                headers = {"X-Vault-Token": self.keys.get("root_token",None)}
            if method == "del":
                res = requests.delete(self.url + url, headers=headers)
            if method == "put":
                res = requests.put(self.url + url, data=json.dumps(data), headers=headers)
            if method == "get" or method == "list":
                res = requests.get(self.url + url, headers=headers)
            if method == "post":
                res = requests.post(self.url + url, data=json.dumps(data), headers=headers)
            ret['status'] = res.status_code
            if isinstance(res.text, dict):
                ret['msg'] = json.loads(res.text)
            else:
                ret['msg'] = res.text
            return ret
        except Exception as ex:
            raise Exception(ex)

    def _SealCheck(self):
        try:
            status = self._postman("/v1/sys/seal-status", method="get")
            if status['status'] == 200:
                if json.loads(status['msg'])['sealed']:
                    self.isUnsealed = False
                    return "Sealed"
                else:
                    self.isUnsealed = True
                    return "Unsealed"
            else:
                raise Exception(status['msg'])
        except Exception as ex:
            raise Exception(ex)

    def VaultInit(self):
        try:
            init_status = self._postman("/v1/sys/init", data=self.initData, method="put")
            if init_status['status'] == 200:
                self._store_keys(init_status['msg'])
            return init_status
        except Exception as ex:
            raise Exception(ex)

    def Seal(self):
        try:
            self._retrieve_keys()
            self._postman("/v1/sys/seal", method="put")
            self.response["data"] = self._SealCheck()
            self.response["status"] = 200
            #return self._SealCheck()
            return self.response
        except Exception as ex:
            self.response["error_msg"] = ex
            self.response["status"] = 400
            return self.response
            #raise Exception(ex)

    def Unseal(self):
        try:
            if not self.isUnsealed:
                self._retrieve_keys()
                unsealData = {}
                unsealData['key'] = self.keys["keys_base64"][-1]
                self._postman("/v1/sys/unseal", data=unsealData, method="put")
            self.response["data"] = self._SealCheck()
            self.response["status"] = 200
            #return self._SealCheck()
            return self.response
        except Exception as ex:
            self.response["error_msg"] = ex
            self.response["status"] = 400
            return self.response
            #raise Exception(ex)

    def Status(self):
        try:
            self.response["data"] = self._postman("/v1/sys/seal-status", method="get")
            self.response["status"] = 200
            #return self._postman("/v1/sys/seal-status", method="get")
            return self.response
        except Exception as ex:
            self.response["error_msg"] = ex
            self.response["status"] = 400
            return self.response
            #raise Exception(ex)

    def _enableAppRole(self):
        try:
            if self.isUnsealed:
                approle = {"type": "approle"}
                return self._postman("/v1/sys/auth/approle", data=approle, method="post")
        except Exception as ex:
            raise Exception(ex)

    def _createPolicy(self, policyName):
        try:
            if self.isUnsealed:
                return self._postman("/v1/sys/policies/acl/" + str(policyName), method="put", data=self.aclPolicy)
        except Exception as ex:
            raise Exception(ex)

    def _enableKV(self):
        try:
            if self.isUnsealed:
                data = { "type":"kv-v1" }
                return self._postman("/v1/sys/mounts/secret", method="post", data=data)
        except Exception as ex:
            raise Exception(ex)

    def _mapRole(self, roleName, policyName):
        try:
            if self.isUnsealed:
                data = {}
                data['policies'] = [policyName]
                return self._postman("/v1/auth/approle/role/" + str(roleName), method="post", data=data)
        except Exception as ex:
            raise Exception(ex)

    def _getRoleID(self, roleName):
        try:
            if self.isUnsealed:
                res = self._postman("/v1/auth/approle/role/" + str(roleName) + "/role-id", method="get")
                if res['status'] == 200:
                    return json.loads(res['msg'])['data']['role_id']
        except Exception as ex:
            raise Exception(ex)

    def _getSecretID(self, roleName):
        try:
            if self.isUnsealed:
                res = self._postman("/v1/auth/approle/role/" + str(roleName) + "/secret-id", method="post")
                if res['status'] == 200:
                    return json.loads(res['msg'])['data']['secret_id']
        except Exception as ex:
            raise Exception(ex)

    def GetRoleID(self, pName , rName):
        try:
            if self.isUnsealed:
                self._enableAppRole()
                self._createPolicy(pName)
                self._enableKV()
                self._mapRole(rName, pName)
                return self._getRoleID(rName)
            else:
                return "Unseal Required"
        except Exception as ex:
            raise Exception(ex)

    def GetRootToken(self):
        try:
            return self.keys["root_token"]
        except Exception as ex:
            raise Exception(ex)

    def addSecret(self,tech,servername="service_account",username=None,password=None):
        try:
            if not username or not password:
                raise Exception("username/password fields are mandatory")
            data = {}
            data['password'] = password
            self.response["data"] = self._postman("/v1/secret/" + str(tech) +  "/" +str(servername)+ "/" + str(username), data=data,  method="post")
            self.response["status"] = 200
            #return self._postman("/v1/secret/" + str(tech) +  "/" +str(servername)+ "/" + str(username), data=data,  method="post")
            return self.response
        except Exception as ex:
            self.response["error_msg"] = ex
            self.response["status"] = 400
            return self.response
            #raise Exception(ex)

    def updateSecret(self,tech,servername,username,password):
        try:
            data = {}
            data['password'] = password
            self.response["data"] = self._postman("/v1/secret/" + str(tech) +"/" +str(servername)+ "/" + str(username), data=data,  method="put")
            self.response["status"] = 200
            #return self._postman("/v1/secret/" + str(tech) +"/" +str(servername)+ "/" + str(username), data=data,  method="put")
            return self.response
        except Exception as ex:
            self.response["error_msg"] = ex
            self.response["status"] = 400
            return self.response
            #raise Exception(ex)

    def delSecret(self,tech,servername, username):
        try:
            self.response["data"] = self._postman("/v1/secret/" + str(tech) + "/"  +str(servername)+ "/" + str(username),  method="del")
            self.response["status"] = 200
            #return self._postman("/v1/secret/" + str(tech) + "/"  +str(servername)+ "/" + str(username),  method="del")
            return self.response
        except Exception as ex:
            self.response["error_msg"] = ex
            self.response["status"] = 400
            return self.response
            #raise Exception(ex)

    def getSecret(self, tech,servername,username):
        try:
            res = self._postman("/v1/secret/" + str(tech) + "/" +str(servername)+ "/" + str(username),  method="get")
            if res['status'] != 200:
                raise Exception(res.get('msg', 'Secret not found'))
            self.response["data"] = json.loads(res["msg"])["data"]
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["data"] = ex
            self.response["status"] = 400
            return self.response

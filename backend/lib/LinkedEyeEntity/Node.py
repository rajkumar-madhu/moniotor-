#####################
##Author : Ponmuthu M
##Version : 18-01-2020
#####################

#Identifier (IP is unique)
#Layer 0 is UI search
#Layer 1 = GatewaySwitch g_swi - svg name
#Layer 2 = Firewall  f_swi - svg name
#Layer 3 = PublicSwitch p_swi - svg name
#Layer 4 = servers [ s_hw , s_sw ]
#Layer 5 = ExchSwitch e_swi - svg name
#{
# "g_swi" : []
# "f_swi" : [.....]
# "servers": ["IP1" : {"sw" : , "hw" : } , "IP2" : {"hw" : } ]
#}

# TEMPORARY compat shim for the Python 3.13 upgrade -- neo4jrestclient's own
# source does `from collections import Sequence`, removed in Python 3.10+
# (it moved to collections.abc in 3.3, then the old alias was dropped
# outright). This is not a real fix: neo4jrestclient is unmaintained since
# 2015 and REST-based Neo4j calls should be migrated to the neo4j Bolt
# driver already imported below -- tracked as a separate, larger task, not
# part of this dependency-version bump.
import collections
import collections.abc
if not hasattr(collections, 'Sequence'):
    collections.Sequence = collections.abc.Sequence

from neo4jrestclient.client import GraphDatabase as RESTGraphDatabase
from neo4j import GraphDatabase as BoltGraphDatabase
try:
    from django.conf import settings
except Exception:
    pass
import json
import os
import re

from lib.LinkedEyeVault.AppSecrets import get_app_secret


def _apply_neo4j_bearer_auth(secure, entity_host=''):
    """Bearer-auth support for Neo4j REST hosts fronted by oauth2-proxy (e.g. a
    public *-neo4j.finspot.in ingress). Internal cluster-only hosts never hit
    oauth2-proxy, so this only activates for secure=True connections; every
    other call re-clears the header first so a stale token never leaks onto an
    unrelated non-bearer connection made later in the same process.

    Reuses the same cached Keycloak client-credentials fetch already used for
    Grafana/Prometheus (lib.LinkedEyeMonitoring.token) rather than duplicating
    the token request/caching logic here.
    """
    try:
        import neo4jrestclient.request as _req
    except Exception:
        return
    if not secure:
        _req.session.headers.pop('Authorization', None)
        # Restore the defaults a prior secure call may have changed below, so
        # trust_env (also gates proxy-env-var resolution, not just auth) isn't
        # left permanently off for internal connections after the first bearer
        # call in this process.
        _req.session.trust_env = True
        _req.session.auth = None
        return
    try:
        from lib.LinkedEyeMonitoring.token import get_neo4j_token
        token = get_neo4j_token(entity_host)
    except Exception:
        token = None
    if token:
        # requests applies netrc-based Basic auth (session.trust_env) and any
        # session.auth tuple *after* merging session.headers, silently
        # overwriting an explicitly-set Authorization header. Clear both so
        # the Bearer token set below is guaranteed to be what goes on the wire.
        _req.session.trust_env = False
        _req.session.auth = None
        _req.session.headers['Authorization'] = 'Bearer ' + token
    else:
        _req.session.headers.pop('Authorization', None)


class Node(object):
    def __init__(self, secure=False, host="", port="", user="", password="", debug=False, bolt=False):
        is_settings = "a_variable" in locals()
        self.response = {'status' : 400 , 'data' : "" , "error_msg" : ""}
        self.isNodeAvailable = False
        self.isLinkAvailable = False
        if user:
            self.user = user
        else:
            if is_settings:
                self.user = settings.NEO4J_USER
            else:
                self.user = os.getenv('NEO4J_USER', 'neo4j')
        if password:
            self.password = password
        else:
            if is_settings:
                self.password = settings.NEO4J_PASS
            else:
                self.password = get_app_secret('NEO4J_PASS', env_var='NEO4J_PASS', default='')
        if not host:
            if is_settings:
                host = settings.NEO4J_HOST
            else:
                host = os.getenv('NEO4J_HOST', 'dev1entity.finspot.in')
        if not port:
            if is_settings:
                port = settings.NEO4J_PORT
            else:
                port = os.getenv('NEO4J_PORT', 7474)
        # DEBUG override: redirect cluster-internal entity host to local port-forward
        # bolt=True → Bolt port (NEO4J_PORT=17687); REST → NEO4J_REST_PORT=17474
        try:
            if settings.DEBUG:
                host = settings.NEO4J_HOST
                port = settings.NEO4J_PORT if bolt else getattr(settings, 'NEO4J_REST_PORT', '17474')
                secure = False
        except Exception:
            pass
        self.host = host
        self.port = port
        self.debug = debug
        self.client = None
        self.defaultprop = {}
        if bolt:
            self.isBolt = True
            self.uri = "bolt://"+str(host)+":"+str(port)
        else:
            self.isBolt = False
            self.proto = "http"
            if secure:
                self.proto = "https"
                # neo4jrestclient attaches self.user/self.password as a per-request
                # HTTP Basic auth tuple (requests.Request(..., auth=(user, password))).
                # requests applies that auth handler *after* merging session headers,
                # so it unconditionally overwrites whatever Authorization header the
                # session already carries -- including a Bearer token set by
                # _apply_neo4j_bearer_auth. Verified directly against requests:
                # session.headers['Authorization']='Bearer x' + a request with
                # auth=('u','p') sends 'Basic ...' on the wire, not the Bearer value.
                # Blank the credentials on the secure/bearer path so neo4jrestclient
                # never attaches Basic auth, leaving Bearer as the only auth sent.
                self.user = ''
                self.password = ''
            self.uri = str(self.proto) + "://" + str(host) + ":" + str(port)
            self._connect()

    def _connect(self):
        try:
            if self.isBolt:
                self.client = BoltGraphDatabase.driver(self.uri, auth=(self.user, self.password))
                #self.client = self.client.session()
                #with self.client as session:
                #result = self.client.run("MATCH (a) RETURN a")
                #for i in result:
                #    print(i)
            else:
                _apply_neo4j_bearer_auth(self.proto == 'https', getattr(self, 'host', ''))
                print("selft.uri={}".format(self.uri))
                self.client = RESTGraphDatabase(self.uri, self.user, self.password)
            return self.client
        except Exception as ex:
            raise Exception("Node connect : Ex = " + str(ex))


    def execute(self, query, delete=False, ret=False):
        try:
            if self.isBolt:
                self._connect()
                with self.client.session() as session:
                    out = session.run(query)
                self.client.close()
            else:
                _apply_neo4j_bearer_auth(self.proto == 'https', getattr(self, 'host', ''))
                out = self.client.query(query)
            if ret:
                return out
            if self.debug:
                print("Query :"+str(query)+" & Output :"+str(out)+" & Length :"+str(len(out)))
            if len(out) or delete:
                self.response["data"] = True
                self.response["status"] = 200
                return self.response
                #return True
            else:
                return self.response
                #return False
        except Exception as ex:
            self.response["error_msg"] = "Node run : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("Node run : Ex = " + str(ex))

    def validateKey(self, prop={}):
        try:
            return json.loads(json.dumps(prop).replace('_NEO4j_',''))
        except Exception as ex:
            raise Exception("Node validateKey : Ex = " + str(ex))

    def addRel(self, parent="", child="", rel_type="RELATED_TO", pLabel='parent'):
        try:
            if not parent or not child:
                raise Exception("Parent & child information are mandatory to make relationship")
            query = "MATCH (a),(b) WHERE a.title = '" + str(parent) + "' AND  b.title = " \
                                                                            "'" + str(
                child) + "'  CREATE (a)-[r:" + str(rel_type) + " { name: a.title + '<->' + " \
                                                               "b.title }]->(b) RETURN r "
            self._checkRel(parent=parent, child=child, rel_type=rel_type)
            if self.isLinkAvailable:
                self.response['status'] = 200
                self.response['data'] = "LinkAlreadyExist"
                return self.response
            if self.execute(query):
                currentParent = self._getparent(child,label=pLabel)
                #if currentParent == "\"\"":
                if currentParent == "\"\"" or not currentParent:
                    currentParentList = []
                else:
                    currentParentList = currentParent.split(',')
                if not parent in currentParentList:
                    currentParentList.append(parent)
                NewParent = ','.join(currentParentList)
                self.update(child,update_key="parent", update_value=NewParent)
                self.response['status'] = 200
                self.response['data'] = 'Success'
                #return "Success"
                return self.response
            else:
                self.response['error_msg'] = "Failed"
                #return "Failed"
                return self.response
        except Exception as ex:
            self.response['error_msg'] = "Node relationship : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("Node relationship : Ex = " + str(ex))

    #process relationship for all servers:
    def addprcRel(self, parent="", child="", rel_type="RELATED_TO", pLabel='parent'):
        try:
            if not parent or not child:
                raise ValueError("Parent and child information are mandatory to create a relationship.")

            # Convert parent and child to lowercase to ensure consistency
            parent = parent.strip().lower()
            child = child.strip().lower()

            # Construct the Cypher query with interpolated values
            query = (
                f"MATCH (a {{title: '{parent}'}}), (b {{title: '{child}'}}) "
                f"CREATE (a)-[r:{rel_type} {{ name: a.title + '<->' + b.title }}]->(b) "
                "RETURN r"
            )

            # Check if the relationship already exists
            self._checkRel(parent=parent, child=child, rel_type=rel_type)
            if self.isLinkAvailable:
                self.response['status'] = 200
                self.response['data'] = "LinkAlreadyExist"
                return self.response

            # Execute the query
            relationship_created = self.execute(query)

            # Handle relationship creation result
            if relationship_created:
                currentParent = self._getparent(child, label=pLabel)
                currentParentList = [] if not currentParent or currentParent == "\"\"" else currentParent.split(',')

                if parent not in currentParentList:
                    currentParentList.append(parent)
                newParent = ','.join(currentParentList)

                self.update(child, update_key="parent", update_value=newParent)
                self.response['status'] = 200
                self.response['data'] = "Success"
            else:
                self.response['status'] = 400
                self.response['data'] = "Failed to create relationship"

            return self.response

        except ValueError as ve:
            self.response['status'] = 400
            self.response['error_msg'] = str(ve)
            return self.response

        except Exception as ex:
            self.response['status'] = 400
            self.response['error_msg'] = f"Node relationship error: {str(ex)}"
            return self.response

    def delRel(self, parent="", child="", rel_type="RELATED_TO", pLabel='parent'):
        try:
            if not parent or not child:
                raise Exception("Parent & child information are mandatory to make relationship")
            self._checkRel(parent=parent, child=child, rel_type=rel_type)
            if not self.isLinkAvailable:
                return "NoLink"
            query = "MATCH (a { title : '" + str(parent) + "' })-[r:" + str(
                rel_type) + "]->(b { title : '" + str(child) + "' }) DELETE r"
            if self.execute(query, delete=True):
                currentParent = self._getparent(child,label=pLabel)
                currentParentList = currentParent.split(',')
                currentParentList.remove(parent)
                NewParent = ','.join(currentParentList)
                if not NewParent:
                    NewParent = "\"\""
                self.update(child, update_key="parent", update_value=NewParent)
                self.response['status'] = 200
                self.response['data'] = 'Success'
                return self.response
                #return "Success"
            else:
                self.response['error_msg'] = "Failed"
                #return "Failed"
                return self.response
        except Exception as ex:
            self.response['error_msg'] = "Node relationship : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("Node relationship : Ex = " + str(ex))

    def _checkRel(self, parent="", child="", rel_type="RELATED_TO"):
        try:
            if not parent or not child:
                raise Exception("Parent & child information are mandatory to make relationship")
            query = "MATCH (a { title : '" + str(parent) + "' })-[r:" + str(
                rel_type) + "]->(b { title : '" + str(child) + "' }) RETURN r"
            out = self.execute(query,ret=True)
            if out:
                self.isLinkAvailable = True
            else:
                self.isLinkAvailable = False
        except Exception as ex:
            raise Exception("Node _checkRel : Ex = " + str(ex))

    def _check(self, title="", key='title', resOut=False):
        try:
            if not title:
                raise Exception("title information is mandatory to _check node")
            query = "MATCH (n { "+str(key)+" : '" + str(title) + "'  } ) return n.title"
            out = self.execute(query, ret=True)
            self.isNodeAvailable = False
            for i in out:
                self.isNodeAvailable = True
                break
            if resOut and self.isNodeAvailable:
                return out[0][0]
        except Exception as ex:
            raise Exception("Node _check : Ex = " + str(ex))

    def _create_prop(self, prop={}):
        try:
            prop = self.validateKey(prop)
            if prop == {}:
                return ""
            _prop = "{  "
            for k, v in prop.items():
                _prop = _prop + str(k) + ":"
                if isinstance(v, int) or isinstance(v, list):
                    _prop = _prop + str(v)
                else:
                    _prop = _prop + "'" + str(v) + "'"
                _prop = _prop + ", "
            _prop = _prop[:-2] + " }"
            return _prop
        except Exception as ex:
            raise Exception("Node _create_prop : Ex = " + str(ex))

    def create(self, prop={}):
        try:
            prop = self.validateKey(prop)
            if not prop:
                raise Exception("property information is mandatory to create node")
            self._check(title=prop['title'])
            if self.isNodeAvailable:
                return self.update(title=prop['title'],bulk_update=prop)
                #self.response['data'] = "AlreadyExist"
                #self.response["status"] = 200
                #return self.response
            prop.update(self.defaultprop)
            _prop = self._create_prop(prop)
            query = "CREATE (a:" + str(prop['type']) + ":" +str(prop['layer'])+ ":ip_" +str(prop['hostIp'].replace('.','_'))+" " + str(_prop) + " ) RETURN a.title"
            # print("query ----=====> " +str(query))
            self.response['data'] = self.execute(query)
            self.response['status'] = 200
            return self.response
            #return self.execute(query)
        except Exception as ex:
            self.response['error_msg'] = "Node create : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("Node create : Ex = " + str(ex))

    def delete_type(self,_type):
        try:
            query = "MATCH (a:" + str(_type) + " ) DETACH DELETE a"
            self.response['data'] = self.execute(query, delete=True)
            self.response['status'] = 200
            return self.response
        except Exception as ex:
            self.response['error_msg'] ="LinkedEyeEntityDB delete_type : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response

    def delete(self, prop={}):
        try:
            prop = self.validateKey(prop)
            _prop = self._create_prop(prop)
            query = "MATCH (a " + str(_prop) + " ) DETACH DELETE a"
            self.response['data'] = self.execute(query, delete=True)
            self.response['status'] = 200
            return self.response
            #return self.execute(query, delete=True)
        except Exception as ex:
            self.response['error_msg'] ="LinkedEyeEntityDB delete : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("LinkedEyeEntityDB delete : Ex = " + str(ex))

    def list(self, prop={}, mode="all"):
        try:
            prop = self.validateKey(prop)
            _prop = self._create_prop(prop)
            query = "MATCH (a " + str(_prop) + " ) return a"
            if mode == "rel":
                query = "match (n),()-[r]-() return n,r"
            out = self.execute(query, ret=True)
            if mode == "all":
                return out
            if mode == "linkedeye":
                _list = []
                if self.isBolt:
                    for _item in out:
                        _list.append(_item["a"])
                elif not self.isBolt:
                    for _item in out:
                        _list.append(_item[0]["data"])
                return _list
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB list : Ex = " + str(ex))

    def stats(self):
        status = {}
        status["total"] = "INF"
        status[0] = "INF"
        status[1] = "INF"
        status[2] = "INF"
        status[3] = "INF"
        status[4] = "INF"
        status[5] = "INF"
        try:
            for item in self.execute("MATCH (n) return count(n)", ret=True):
                status["total"] = item[0]
            for item in self.execute("MATCH (n {status :0}) return count(n)", ret=True):
                status[0] = item[0]
            for item in self.execute("MATCH (n {status :1}) return count(n)", ret=True):
                status[1]= item[0]
            for item in self.execute("MATCH (n {status :2}) return count(n)", ret=True):
                status[2] = item[0]
            for item in self.execute("MATCH (n {status :3}) return count(n)", ret=True):
                status[3] = item[0]
            for item in self.execute("MATCH (n {status :4}) return count(n)", ret=True):
                status[4] = item[0]
            for item in self.execute("MATCH (n {status :5}) return count(n)", ret=True):
                status[5] = item[0]
            return status
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB latest : Ex = " + str(ex))

    def update_checkStatus(self, title="", update_key="", update_value="", bulk_update={}):

        try:
            if 'overviewstats' in bulk_update:
                del bulk_update['overviewstats']
        except Exception as ex:
            print("overviewstats key delete Issue ")
        try:
            update_key = update_key.replace('_NEO4j_', '')
            bulk_update = self.validateKey(bulk_update)
            if not title:
                return "NothingToUpdate"
            if not bulk_update:
                if not update_key or not update_value:
                    return "NothingToUpdate"
                dict = {update_key: update_value}
            else:
                dict = bulk_update
            SET_STR = self._create_prop(dict)
            if update_value == "" and 'type' in dict and 'layer' in dict and 'hostIp' in dict:
                query = "MATCH (n  {title : '" + str(title) + "' , type : '"+str(dict['type'])+"' , layer : '"+str(dict['layer'])+"' , hostIp : '"+str(dict['hostIp'])+"' }) SET n += " + str(SET_STR) + "  RETURN n"
            else:
                query = "MATCH (n  {title : '" + str(title) + "' }) SET n += " + str(SET_STR) + "  RETURN n"
            status_query = "MATCH (n  {title : '" + str(title) + "' }) RETURN n.status"
            oldState_out = self.execute(status_query, ret=True)
            oldState=99
            newState=98
            for _item in oldState_out:
                oldState = _item
            self.execute(query)
            newState_out = self.execute(status_query, ret=True)
            #print("oldState_out {} newState_out {}".format(oldState_out,newState_out))
            for _item in newState_out:
                newState = _item
            #print("oldState {} newState{}".format(oldState,newState))
            if oldState == newState:
                return False
            return True
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB update : Ex = " + str(ex))

    def update(self, title="", update_key="", update_value="", bulk_update={}):
        #print("Update bulk_update = {}".format(bulk_update))
        try:
            if 'overviewstats' in bulk_update:
                del bulk_update['overviewstats']
        except Exception as ex:
            print("overviewstats key delete Issue ")
        try:
            update_key = update_key.replace('_NEO4j_', '')
            bulk_update = self.validateKey(bulk_update)
            if not title:
                return "NothingToUpdate"
            if not bulk_update:
                if not update_key or not update_value:
                    return "NothingToUpdate"
                dict = {update_key: update_value}
            else:
                dict = bulk_update
            SET_STR = self._create_prop(dict)
            #query = "MATCH (n {title : '" + str(title) + "' }) SET n += " + str(SET_STR) + " RETURN n"
            #query = "MATCH (n  {title : '" + str(title) + "' }) SET n += " + str(SET_STR) + " , n :"+str(dict['type'])+" :"+str(dict['layer'])+" :"+str(dict['hostIp'].replace('.','_'))+"  RETURN n"
            if update_value == "" and 'type' in dict and 'layer' in dict and 'hostIp' in dict:
                query = "MATCH (n  {title : '" + str(title) + "' , type : '"+str(dict['type'])+"' , layer : '"+str(dict['layer'])+"' , hostIp : '"+str(dict['hostIp'])+"' }) SET n += " + str(SET_STR) + "  RETURN n"
            else:
                query = "MATCH (n  {title : '" + str(title) + "' }) SET n += " + str(SET_STR) + "  RETURN n"
            #print("Query = {}".format(query))
            return self.execute(query)
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB update : Ex = " + str(ex))

    def _getparent(self, node, label='parent'):
        try:
            query = "MATCH (n {title : '" + str(node) + "' })  RETURN n."+str(label)
            out = self.execute(query, ret=True)
            for i in out: #Note : n returns only one node
                return i[0]
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB _getparent : Ex = " + str(ex))

    def _visspecificnodedata(self, mode="All", ip="All"): #Mode Types : All, s_sw, g_swi, p_swi, s_hw etc
        pro = "(n"
        if mode == "All":
            pro = pro + ")"
        else:
            pro = pro + ":" + mode + ")"
        if not ip == "All":
            pro = pro.replace(')','') + ":" + ip + ")"
        query = "MATCH "+pro+" RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink , n.source_port, n.datetime, n.automation_workflow, n.colon"
        # query = "MATCH "+pro+" RETURN ID(n), n.title, n.mon_status, n.mon_message, n.type, n.icon, n.datetime, n.address, n.dashboard"
        # print(query)
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _visspecificnodedata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("LinkedEyeEntityDB _visspecificnodedata : Ex = " + str(ex))

    def _visspecificrelationshipdata(self, mode="All", ip="All"): #Mode Types : All, s_sw, g_swi, p_swi, s_hw etc
        if not ip == "All" and not mode == "All": ## ip with compulsary layer logic
            pro_s = "src {layer: \""+str(mode)+"\" , hostIp: \""+str(ip)+"\"}"
            pro_d = "dst {layer: \""+str(mode)+"\" , hostIp: \""+str(ip)+"\"}"
        elif mode == "All": ## all
            pro_s = ""
            pro_d = ""
        elif not mode == "All" and ip == "All": ## only layer logic
            pro_s = "src {layer: \""+str(mode)+"\"}"
            pro_d = "dst {layer: \""+str(mode)+"\"}"

        #query = "MATCH ()-[r]-() RETURN ID(startNode(r)), ID(endNode(r)), type(r)"
        query = "MATCH ("+str(pro_s)+")-[r]-("+str(pro_d)+") RETURN ID(startNode(r)), ID(endNode(r)), type(r)"
        #print("_visspecificrelationshipdata -->"+str(query))
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _visspecificrelationshipdata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("LinkedEyeEntityDB _visspecificrelationshipdata : Ex = " + str(ex))

    def _getdashboardspecificdata(self, mode="Host"):
        query = "MATCH (n:"+mode+") return n.monitor_status, count(n.monitor_status)"
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _getdashboardspecificdata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("LinkedEyeEntityDB _getdashboardspecificdata : Ex = " + str(ex))

    def _getdashboarddata(self, datafor='Host'):
        try:
            if datafor == 'Host':
                query = "MATCH (n) where n.kind=~'(?i)HOST' or n.kind=~'(?i)NODE' return n.monitor_status, count(n.monitor_status)"
            else:
               query = "MATCH (n) where n.kind=~'(?i)HOSTMS' or n.kind=~'(?i)SERVICE' or n.kind=~'(?i)SERVICEMS' or n.kind=~'(?i)POD' return n.monitor_status, count(n.monitor_status)"
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _getdashboarddata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response

    def _getSpecificNodeDetails(self, nodeid, mode="",ipid=""):
        # query = "MATCH(n) WHERE ID(n) = "+nodeid+" return (n)"

        #print("TESTING====> {} {} ".format(nodeid, mode))
        _filter = ""
        if not ipid == "":
            _filter = ":"+str(ipid)
        regex = re.compile('[-.:_]')
        isTrue = False
        if(regex.search(nodeid) == None):
            if mode == "name":
                #query = "MATCH(n"+_filter+") WHERE n.name=~'(?i)"+nodeid+"' OR n.name=~'(?i)"+nodeid+".*' return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
                query = (
        "MATCH(n" + _filter + ") "
        "WHERE n.name=~'(?i)^" + nodeid + "$' OR n.name=~'(?i).*[:]" + nodeid + "$' "
        "RETURN Id(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp, n.overlayIP, n.dashboard"
    )
            else:
                isTrue = True
                query = "MATCH(n"+_filter+") WHERE ID(n) = "+nodeid+" return (n)"
        else:
            if(nodeid == "pills-ok"):
                query = "MATCH(n"+_filter+") WHERE n.status=2 return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
            elif(nodeid == "pills-critical"):
                query = "MATCH(n"+_filter+") WHERE n.status=0 return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
           # elif(nodeid == "pills-pending"):
           #     query = "MATCH(n"+_filter+") WHERE n.monitor_status=~'(?i)PENDING' return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
            elif(nodeid == "pills-warning"):
                query = "MATCH(n"+_filter+") WHERE n.status=1 return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
            elif(nodeid == "pills-unknown"):
                query = "MATCH(n"+_filter+") WHERE n.status=3 return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
            else:
                query = "MATCH(n"+_filter+") WHERE n.name=~'(?i)"+nodeid+"' OR n.name=~'(?i)"+nodeid+".*' return Id(n) ,n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard"
        #print('QUERY SET--->'+ str(query))
        try:
            out = self.execute(query, ret=True)
            _list = []
            if(isTrue):
                _list.append(out[0][0]["data"])
            else:
                for _item in out:
                    _list.append(_item)
            # _list.append(out[0][0]["data"])
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _getSpecificNodeDetails : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("LinkedEyeEntityDB _getSpecificNodeDetails : Ex = " + str(ex))


    def set_downtime(self,title):
        try:
            query = "MATCH (n {title : '" + str(title) + "' }) SET n.downtime=1 return n"
            return self.execute(query)
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB set_downtime : Ex = " + str(ex))

    def unset_downtime(self,title):
        try:
            query = "MATCH (n {title : '" + str(title) + "' }) SET n.downtime=0 return n"
            return self.execute(query)
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB set_downtime : Ex = " + str(ex))


    def layerwiseCount(self):
        filedata=open("iframeGraphs/neochart-query.json")
        filecontent=json.loads(filedata.read())
        filedata.close()
        newresponse={}
        try:
            for key,value in filecontent.items():
                out = self.execute(value, ret=True)
                _list = []
                for _item in out:
                    _list.append(_item)
                newresponse[key]=_list
                #self.response[] = _list
                newresponse["status"] = 200
            return newresponse
            #return _list
        except Exception as ex:
            newresponse["error_msg"] = "LinkedEyeEntityDB _getChartDetails : Ex = " + str(ex)
            newresponse["status"] = 400
            return newresponse



     #client = Node(....)
    #client.getSpecificElement(title="172.16.0.2:p06", reqEle="ifIndex")
    #client.getSpecificElement(title="172.16.0.2:p06")
    def getSpecificElement(self, title, reqEle="ifIndex"):
        try:
            ret = {}
            query = "MATCH (n {title : '" + str(title) + "' })  RETURN n.link,n.portname,n.destname"
            #query = "MATCH (n {title : '" + str(title) + "' })  RETURN n."+str(reqEle)
            out = self.execute(query, ret=True)
            for i in out: #Note : n returns only one node
                ret['staus'] = 200
                ret['data'] = i
                #ret['data'] = i[0]
            return ret
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB getSpecificelement : Ex = " + str(ex))


    def overviewStats(self):
        try:
            response={}
            status = {}
            iplist = self.execute("MATCH (n) WHERE (n.kind='Node' OR n.kind='Host') RETURN n.hostIp", ret=True)
            #iplist = self.execute("MATCH (n:Host) RETURN n.hostIp", ret=True)
            for ip in iplist:
                #ip = _ip[0]
                ip=ip[0]
                status[ip] = {}
                status[ip]["hardware"] = {}
                status[ip]["software"] = {}
                status[ip]["application"] = {}
                for item in self.execute("MATCH (n {hostIp:\""+ip+"\"}) return count(n)", ret=True):
                    status[ip]["total"] = item[0]

                ##############HARDWARE DATA##################
                #print("QUERY---> MATCH (n {type : 'Host',status : 0,ip :\""+ip+"\" }) return distinct count(n)")
                for item in self.execute("MATCH (n {label : 'hardware',status : 0,hostIp :\""+ip+"\" }) return  count(n)", ret=True):
                    status[ip]['hardware'][0] = item[0]
                for item in self.execute("MATCH (n {label : 'hardware',status : 1,hostIp :\""+ip+"\" }) return  count(n)", ret=True):
                    status[ip]['hardware'][1] = item[0]
                for item in self.execute("MATCH (n {label : 'hardware',status : 2,hostIp :\""+ip+"\" }) return  count(n)", ret=True):
                    status[ip]['hardware'][2] = item[0]
                for item in self.execute("MATCH (n {label : 'hardware',status : 3,hostIp :\""+ip+"\" }) return  count(n)", ret=True):
                    status[ip]['hardware'][3] = item[0]
                ###############SOFTWARE DATA###############
                for item in self.execute("MATCH (n {label: 'software',status: 0,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['software'][0]=item[0]
                for item in self.execute("MATCH (n {label: 'software',status: 1,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['software'][1]=item[0]
                for item in self.execute("MATCH (n {label: 'software',status: 2,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['software'][2]=item[0]
                for item in self.execute("MATCH (n {label: 'software',status: 3,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['software'][3]=item[0]
                ###############APPLICATION DATA###############
                for item in self.execute("MATCH (n {label: 'application',status: 0,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['application'][0]=item[0]
                for item in self.execute("MATCH (n {label: 'application',status: 1,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['application'][1]=item[0]
                for item in self.execute("MATCH (n {label: 'application',status: 2,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['application'][2]=item[0]
                for item in self.execute("MATCH (n {label: 'application',status: 3,hostIp :\""+ip+"\" }) return count(n)", ret=True):
                    status[ip]['application'][3]=item[0]

            response['status']=200
            response['data']=status

            return response
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB overviewStats : Ex = " + str(ex))
        
    def websocketStats(self,ip=''):#Newly added (27-09-2023) - this is for websocket publishing
        try:
            response={}
            status = {}
            status['overview'] = {}
            status['overview'][ip] = {}
            status['overall'] = {}
            status['overall']["hardware"] = {}
            status['overall']['hardware'][0]=0
            status['overall']['hardware'][1]=0
            status['overall']['hardware'][2]=0
            status['overall']['hardware'][3]=0
            status['overall']["software"] = {}
            status['overall']['software'][0]=0
            status['overall']['software'][1]=0
            status['overall']['software'][2]=0
            status['overall']['software'][3]=0
            status['overall']["application"] = {}
            status['overall']['application'][0]=0
            status['overall']['application'][1]=0
            status['overall']['application'][2]=0
            status['overall']['application'][3]=0
            ips='ip_'+ip.replace('.','_')
            query1="MATCH (n:"+ips+") WHERE n.status IN [0, 1, 2, 3, 4] WITH n.status AS status, COUNT(n) AS count RETURN status, count"
            #query1="MATCH (n {hostIp :\""+ip+"\"}) WHERE n.label IN ['hardware', 'software', 'application'] RETURN n.label, n.status, COUNT(*) AS count ORDER BY n.label, n.status"
            for item in self.execute(query1, ret=True):#overview stats
                #print(item)
                status['overview'][ip][item[0]]=[item[1]] 
                
            query2="""MATCH (n) WHERE n.label IN ['hardware', 'software', 'application'] RETURN n.label, n.status, COUNT(*) AS count ORDER BY n.label, n.status"""
            for item in self.execute(query2, ret=True):#overall stats
                #print(item)
                status['overall'][item[0]][item[1]] = item[2]
                
            response['status']=200
            response['data']=status

            return response
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB websocketStats : Ex = " + str(ex))

    def overallStats(self):
        try:
            response={}
            status = {}
            #iplist = self.execute("MATCH (n) WHERE (n.kind='Node' OR n.kind='Host') RETURN n.hostIp", ret=True)
            #iplist = self.execute("MATCH (n:Host) RETURN n.hostIp", ret=True)
            status["hardware"] = {}
            status['hardware'][0]=0
            status['hardware'][1]=0
            status['hardware'][2]=0
            status['hardware'][3]=0
            status["software"] = {}
            status['software'][0]=0
            status['software'][1]=0
            status['software'][2]=0
            status['software'][3]=0
            status["application"] = {}
            status['application'][0]=0
            status['application'][1]=0
            status['application'][2]=0
            status['application'][3]=0
            #for ip in iplist:
                #ip = _ip[0]
                #ip=ip[0]
            ##############HARDWARE DATA##################
            #print("QUERY---> MATCH (n {type : 'Host',status : 0 }) return distinct count(n)")
            for item in self.execute("MATCH (n {label : 'hardware',status : 0 }) return  count(n)", ret=True):
                status['hardware'][0] =status['hardware'][0]+ item[0]
            for item in self.execute("MATCH (n {label : 'hardware',status : 1 }) return  count(n)", ret=True):
                status['hardware'][1] =status['hardware'][1]+ item[0]
            for item in self.execute("MATCH (n {label : 'hardware',status : 2 }) return  count(n)", ret=True):
                status['hardware'][2] =status['hardware'][2]+item[0]
            for item in self.execute("MATCH (n {label : 'hardware',status : 3 }) return  count(n)", ret=True):
                status['hardware'][3] =status['hardware'][3]+ item[0]
            ###############SOFTWARE DATA###############
            for item in self.execute("MATCH (n {label: 'software',status: 0 }) return count(n)", ret=True):
                status['software'][0]=status['software'][0] + item[0]
            for item in self.execute("MATCH (n {label: 'software',status: 1 }) return count(n)", ret=True):
                status['software'][1]=status['software'][1] + item[0]
            for item in self.execute("MATCH (n {label: 'software',status: 2 }) return count(n)", ret=True):
                status['software'][2]=status['software'][2]+item[0]
            for item in self.execute("MATCH (n {label: 'software',status: 3 }) return count(n)", ret=True):
                status['software'][3]=status['software'][3]+item[0]
            ###############APPLICATION DATA###############
            for item in self.execute("MATCH (n {label: 'application',status: 0 }) return count(n)", ret=True):
                status['application'][0]=status['application'][0] + item[0]
            for item in self.execute("MATCH (n {label: 'application',status: 1 }) return count(n)", ret=True):
                status['application'][1]=status['application'][1] + item[0]
            for item in self.execute("MATCH (n {label: 'application',status: 2 }) return count(n)", ret=True):
                status['application'][2]=status['application'][2]+item[0]
            for item in self.execute("MATCH (n {label: 'application',status: 3 }) return count(n)", ret=True):
                status['application'][3]=status['application'][3]+item[0]

            response['status']=200
            response['data']=status

            if status['hardware'][0] > 0 or status['software'][0] > 0 or status['application'][0] > 0 :
                 response['isEntityRED'] = 0
            elif status['hardware'][1] > 0 or status['software'][1] > 0 or status['application'][1] > 0 :
                response['isEntityRED'] = 1
            elif status['hardware'][2] > 0 or status['software'][2] > 0 or status['application'][2] > 0 :
                response['isEntityRED'] = 2
            else:
                response['isEntityRED'] = 3
            # only HeatMap red/Green is processed
            return response
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB overviewStats : Ex = " + str(ex))
        
    def overallDashStats(self):#Newly added (27-09-2023)
        try:
            response={}
            status = {}
            status["hardware"] = {}
            status['hardware'][0]=0
            status['hardware'][1]=0
            status['hardware'][2]=0
            status['hardware'][3]=0
            status["software"] = {}
            status['software'][0]=0
            status['software'][1]=0
            status['software'][2]=0
            status['software'][3]=0
            status["application"] = {}
            status['application'][0]=0
            status['application'][1]=0
            status['application'][2]=0
            status['application'][3]=0
            query="""
                    MATCH (n)
                    WHERE n.label IN ['hardware', 'software', 'application']
                    RETURN n.label, n.status, COUNT(*) AS count
                    ORDER BY n.label, n.status
                  """
            for item in self.execute(query, ret=True):
                #print(item)
                status[item[0]][item[1]] = item[2]
            
            response['status']=200
            response['data']=status

            if status['hardware'][0] > 0 or status['software'][0] > 0 or status['application'][0] > 0 :
                 response['isEntityRED'] = 0
            elif status['hardware'][1] > 0 or status['software'][1] > 0 or status['application'][1] > 0 :
                response['isEntityRED'] = 1
            elif status['hardware'][2] > 0 or status['software'][2] > 0 or status['application'][2] > 0 :
                response['isEntityRED'] = 2
            else:
                response['isEntityRED'] = 3
            # only HeatMap red/Green is processed
            return response
        except Exception as ex:
            raise Exception("LinkedEyeEntityDB overviewStats : Ex = " + str(ex))



    def specificrelationshipdata(self, title=""):
        #query = "MATCH ()-[r]-() RETURN ID(startNode(r)), ID(endNode(r)), type(r)"
        child_query = "MATCH (n:s_sw {title:'"+str(title)+"'})-[r]-() RETURN ID(startNode(r)), ID(endNode(r)), type(r) , (startNode(r).title) , (endNode(r).title)"
        parent_query = "MATCH ()-[r]-(n:s_sw {title:'"+str(title)+"'}) RETURN ID(startNode(r)), ID(endNode(r)), type(r) , (startNode(r).title) , (endNode(r).title)"
        #print("CHILD QUERY -->"+str(child_query))
        #print("PARENT QUERY -->"+str(parent_query))
        try:
            _list = []
            out = self.execute(child_query, ret=True)
            for _item in out:
                _list.append(_item)
            out = self.execute(parent_query, ret=True)
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB specificrelationshipdata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
            #raise Exception("LinkedEyeEntityDB _visspecificrelationshipdata : Ex = " + str(ex))


    def getcomparisiondataTesting(self, type="",query_created=''):
        configmap_query = query_created
        #configmap_query = "MATCH (n:configmap) RETURN n"
        versions_query = "MATCH ()-[r]-(n:s_sw ) RETURN ID(startNode(r)), ID(endNode(r)), type(r) , (startNode(r).title) , (endNode(r).title)"
        env_query = "MATCH ()-[r]-(n:s_sw ) RETURN ID(startNode(r)), ID(endNode(r)), type(r) , (startNode(r).title) , (endNode(r).title)"
        try:
            _list = []
            query=''
            if(type=='configmap'):
                query=configmap_query
                out = self.execute(query, ret=True)
                for _item in out:
                    _list.append(_item[0]['data'])
                    #print(_item[0]['data'])
            elif(type=='versions'):
                query=versions_query
                out = self.execute(query, ret=True)
                for _item in out:
                    _list.append(_item[0])
            elif(type=='envs'):
                query=env_query
                out = self.execute(query, ret=True)
                for _item in out:
                    _list.append(_item[0])
            #out = self.execute(query, ret=True)

            self.response["status"] = 200
            self.response["data"] = _list
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB getcomparisiondata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response


    def getcomparisiondata(self, type="",query_created=''):
        queries = query_created
        #configmap_query = "MATCH (n:configmap) RETURN n"
        #versions_query = "MATCH ()-[r]-(n:s_sw ) RETURN ID(startNode(r)), ID(endNode(r)), type(r) , (startNode(r).title) , (endNode(r).title)"
        #env_query = "MATCH ()-[r]-(n:s_sw ) RETURN ID(startNode(r)), ID(endNode(r)), type(r) , (startNode(r).title) , (endNode(r).title)"
        try:
            _list = []
            query=''
            query=queries
            out = self.execute(query, ret=True)
            for _item in out:
                _list.append(_item[0]['data'])
                #print(_item[0]['data'])

            self.response["data"] = _list
            return self.response
            #return _list
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB getcomparisiondata : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response

    def _visallhost(self, mode="All"): 
        query = "MATCH (n) where n.type='Host' RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink,n.device_type,n.tech, n.automation_workflow, n.colon"
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _vishost : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        
    def _vishost(self, mode="All"): 
        query = "MATCH (n) where n.type='Host' AND (n.tech='Linux' OR n.tech='Windows' OR n.tech='Switch') RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink,n.device_type,n.tech,n.ips_list, n.automation_workflow, n.colon"
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _vishost : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        
    def _visicon(self, mode="All"): 
        query = "MATCH (n) where ( n.type='HostMS' OR n.type='ServiceMS') AND (NOT n.image='port') RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink, n.automation_workflow, n.colon"
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _visicon : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        
    def _visspecificicon(self, mode="All",ip=''): 
        _filter = ""
        """if not ip == "":
            _filter = ":"+str(ip)"""
            
        query = "MATCH (n) where (n.ip IN "+ip+") AND ( n.type='HostMS' OR n.type='ServiceMS') AND (NOT n.image='port') RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink,n.stats,n.datetime, n.automation_workflow, n.colon"
        #query = "MATCH (n"+_filter+") where ( n.type='HostMS' OR n.type='ServiceMS') AND (NOT n.image='port') RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink"
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _visicon : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response


    def _visnicconnect(self): 
        query = "MATCH (n) where not (n.Phy_Physicalniclink='') RETURN ID(n), n.title, n.monitor_status, n.monitor_message, n.type, n.image, n.epoch, n.hostIp,n.overlayIP, n.dashboard, n.link, n.status, n.Friendly_name, n.Nics_list, n.Phy_Physicalip, n.DiskVolumes_list, n.Phy_Physicalniclink, n.automation_workflow, n.colon"
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _visicon : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        
    def _getstatusAll(self): 
        query = """ MATCH (n)
                    WHERE (n.kind='Pod' OR n.tech='Linux' OR n.tech='Windows' OR n.tech='Switch') AND n.status IN [0, 1, 2, 3, 4]
                    WITH n.hostIp AS title, n.status AS status, COUNT(*) AS count
                    ORDER BY title, status
                    WITH title, COLLECT("{" + toString(status) + ":" + count + "}") AS statusCounts
                    RETURN title+':'+ "[" + REDUCE(s = "", x IN statusCounts | s + CASE WHEN s = "" THEN x ELSE "," + x END) + "]" AS statusCountsString;
                """
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _getstatusAll : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        
    def set_link(self,mode,data):
        query = ""
        
        try:
            if(mode=='update'):
                if('prev_src_ip' in data):
                    query = "MATCH (n {title : '" + str(data['prev_src_ip']) + str(data['prev_src_port']) + "' }) SET n.link=null, n.portname=null ,n.destname=null return n"
                    self.execute(query, ret=True)
                    query = "MATCH (n {title : '" + str(data['prev_dest_ip']) +str(data['prev_dest_port']) + "' }) SET  n.portname=null , n.destname=null return n"
                    self.execute(query, ret=True)
                
                query = "MATCH (n {title : '" + str(data['source_ip']) + str(data['source_port']) + "' }) SET n.link='"+str(data['dest_ip']) + str(data['dest_port']) +"', n.portname='"+str(data['source_name'])+"' , n.destname='"+str(data['dest_name'])+"' return n"
                #query = "MATCH (n {title : '" + str(data['source_ip']) + str(data['source_port']) + "' }) SET n.link='"+str(data['dest_ip']) + str(data['dest_port']) +"', n.portname='"+str(data['source_name'])+"' , n.destname='"+str(data['dest_name'])+"' , n.source_port ='"+str((data['source_port']).replace(":",""))+"' return n"
                self.execute(query, ret=True)
                query = "MATCH (n {title : '" + str(data['dest_ip'])  +str(data['dest_port']) + "' }) SET  n.portname='"+str(data['dest_name'])+"' , n.destname='"+str(data['source_name'])+"' return n"
                self.execute(query, ret=True)
            elif(mode=='delete'):
                query = "MATCH (n {title : '" + str(data['source_ip']) + str(data['source_port']) + "' }) SET  n.link=null, n.portname=null ,n.destname=null return n"
                self.execute(query, ret=True)
                query = "MATCH (n {title : '" + str(data['dest_ip'])  +str(data['dest_port']) + "' }) SET  n.link=null, n.portname=null ,n.destname=null return n"
                self.execute(query, ret=True)
                
            out = self.execute(query, ret=True)
            self.response["data"] = out
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntity set_link error : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        


    def _getswitchesAll(self): 
        query = """ MATCH (n) where n.type = 'Host'AND n.tech = 'Switch' RETURN n.title , n.image
                """
        
        try:
            out = self.execute(query, ret=True)
            _list = []
            for _item in out:
                _list.append(_item)
            self.response["data"] = _list
            self.response["status"] = 200
            return self.response
        except Exception as ex:
            self.response["error_msg"] = "LinkedEyeEntityDB _getswitchesAll : Ex = " + str(ex)
            self.response["status"] = 400
            return self.response
        

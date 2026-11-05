"""
Dashboard service layer (PREVENTIVE FIX #27).

Encapsulates business logic for Neo4j queries, multi-site aggregation,
SNMP configuration, and websocket stat retrieval.  Functions accept plain
Python data and return dicts -- never Django request/response objects.
"""

import json
import logging
import yaml

from django.contrib.auth.models import User
from django.db import connection
from django.conf import settings

from lib.LinkedEyeEntity.Node import Node
from lib.LinkedEyeValidation import snmp
from lesites.models import SiteModel
from useronboard.models import Usersite
from dashboard.models import SnmpModel
from allonboard.models import allonboardModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Neo4j node queries
# ---------------------------------------------------------------------------

def get_neo4j_nodes(site_config, layer="All", ip="All"):
    """Fetch Neo4j visualisation data (nodes + relationships) for a site.

    Args:
        site_config (dict): Must contain ``entity_host``, ``entity_port``,
            ``is_URLSecured``, and ``sitename``.
        layer (str): Node layer filter (default ``"All"``).
        ip (str): IP filter (default ``"All"``).

    Returns:
        dict: ``{'site': str, 'site_data': dict, 'code': int}``
    """
    temp_obj = {'site': site_config['sitename']}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        node_response = {}
        rel_response = {}

        node_res = nodeObj._visspecificnodedata(mode=layer, ip=ip)
        if node_res['status'] == 200:
            node_response['status'] = node_res['status']
            node_response['data'] = node_res['data']
        else:
            node_response['status'] = node_res['status']

        clean_ip = ip.split('ip_')[-1].replace('_', '.')
        rel_res = nodeObj._visspecificrelationshipdata(mode=layer, ip=clean_ip)
        if rel_res['status'] == 200:
            rel_response['status'] = rel_res['status']
            rel_response['data'] = rel_res['data']
        else:
            rel_response['status'] = rel_res['status']

        temp_obj['site_data'] = {"nodes": node_response, "relationships": rel_response}
        temp_obj['code'] = 200
    except Exception as e:
        logger.exception("get_neo4j_nodes failed for site=%s", site_config.get('sitename'))
        temp_obj['site_data'] = {}
        temp_obj['code'] = 500
    return temp_obj


def get_neo4j_nodes_for_user(username, layer="All", ip="All"):
    """Fetch Neo4j nodes across all sites the user has access to.

    Args:
        username (str): Django username.
        layer (str): Node layer filter.
        ip (str): IP filter.

    Returns:
        dict: ``{'status': 200, 'responseData': list[dict]}``
    """
    user_id = User.objects.get(username=username).id
    site_ids = list(Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True))
    site_objs = SiteModel.objects.filter(id__in=site_ids, is_enable=True)

    data = []
    for site_obj in site_objs:
        cfg = {
            'entity_host': site_obj.entity_host,
            'entity_port': site_obj.entity_port,
            'is_URLSecured': site_obj.is_URLSecured,
            'sitename': site_obj.sitename,
        }
        data.append(get_neo4j_nodes(cfg, layer, ip))
    return {"status": 200, "responseData": data}


# ---------------------------------------------------------------------------
# Overall / overview chart data
# ---------------------------------------------------------------------------

def get_overall_chart_data(site_config, all_site=False):
    """Aggregate chart statistics for a site.

    Args:
        site_config (dict): Site connection details.
        all_site (bool): If ``True``, aggregate across all sub-sites.

    Returns:
        dict: ``{'status': int, 'data': dict, 'site': str}`` or error.
    """
    response = {}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )

        if all_site:
            hw = {'CRITICAL': 0, 'WARNING': 0, 'OK': 0, 'UNKNOWN': 0}
            sw = {'CRITICAL': 0, 'WARNING': 0, 'OK': 0, 'UNKNOWN': 0}
            app = {'CRITICAL': 0, 'WARNING': 0, 'OK': 0, 'UNKNOWN': 0}

            result = nodeObj.overallStats()
            if result['status'] == 200:
                d = result['data']
                hw['CRITICAL'] += d['hardware'][0]
                hw['WARNING'] += d['hardware'][1]
                hw['OK'] += d['hardware'][2]
                hw['UNKNOWN'] += d['hardware'][3]
                sw['CRITICAL'] += d['software'][0]
                sw['WARNING'] += d['software'][1]
                sw['OK'] += d['software'][2]
                sw['UNKNOWN'] += d['software'][3]
                app['CRITICAL'] += d['application'][0]
                app['WARNING'] += d['application'][1]
                app['OK'] += d['application'][2]
                app['UNKNOWN'] += d['application'][3]

            response['site'] = site_config['sitename']
            response['status'] = result['status']
            response['data'] = {"hardware": hw, "software": sw, "application": app}
        else:
            result = nodeObj.overviewStats()
            if result['status'] == 200:
                response['status'] = result['status']
                response['data'] = result['data']
            else:
                response['status'] = result['status']
    except Exception as e:
        logger.exception("get_overall_chart_data failed for site=%s", site_config.get('sitename'))
        response['error_msg'] = f"overallchartdetails : Ex = {str(e)}"
        response['status'] = 400
    return response


# ---------------------------------------------------------------------------
# Host / service detail queries
# ---------------------------------------------------------------------------

def get_host_service_details(site_config=None, mode="Host"):
    """Query dashboard-specific host or service data from Neo4j.

    Args:
        site_config (dict|None): If provided, use specific site.  Otherwise
            uses default ``Node()`` connection.
        mode (str): One of ``"Host"``, ``"Service"``, ``"HostMS"``,
            ``"ServiceMS"``.

    Returns:
        dict: ``{'status': int, 'data': ...}``
    """
    response = {}
    try:
        if site_config:
            nodeObj = Node(
                host=site_config['entity_host'],
                port=site_config['entity_port'],
                secure=site_config['is_URLSecured'],
            )
        else:
            nodeObj = Node()

        result = nodeObj._getdashboardspecificdata(mode=mode)
        response['status'] = result['status']
        if result['status'] == 200:
            response['data'] = result['data']
    except Exception as e:
        logger.exception("get_host_service_details failed (mode=%s)", mode)
        response['status'] = 400
        response['error_msg'] = str(e)
    return response


def get_host_service_count(site_config=None):
    """Return dashboard host and service counts.

    Returns:
        dict: ``{'host': dict, 'service': dict}``
    """
    nodeObj = Node() if not site_config else Node(
        host=site_config['entity_host'],
        port=site_config['entity_port'],
        secure=site_config['is_URLSecured'],
    )

    host_response = {}
    service_response = {}

    host_result = nodeObj._getdashboarddata(datafor='Host')
    if host_result['status'] == 200:
        host_response['status'] = host_result['status']
        host_response['data'] = host_result['data']
    else:
        host_response['status'] = host_result['status']

    service_result = nodeObj._getdashboarddata(datafor="Service")
    if service_result['status'] == 200:
        service_response['status'] = service_result['status']
        service_response['data'] = service_result['data']
    else:
        service_response['status'] = service_result['status']

    return {"host": host_response, "service": service_response}


def get_node_specific_details(nodeid, mode, site_config, ip):
    """Fetch details of a specific Neo4j node.

    Returns:
        dict: ``{'status': int, 'data': ...}``
    """
    response = {}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        result = nodeObj._getSpecificNodeDetails(nodeid, mode, ip)
        response['status'] = result['status']
        if result['status'] == 200:
            response['data'] = result['data']
    except Exception as e:
        logger.exception("get_node_specific_details failed for nodeid=%s", nodeid)
        response['error_msg'] = f"getnodespecificdetails : Ex = {str(e)}"
        response['status'] = 400
    return response


# ---------------------------------------------------------------------------
# Websocket update
# ---------------------------------------------------------------------------

def get_websocket_update(site_config, ip=None):
    """Fetch websocket statistics from Neo4j.

    Args:
        site_config (dict): Site connection details.
        ip (str): IP address to query.

    Returns:
        dict: ``{'status': int, 'data': {'overview': ..., 'overall': ...}}``
    """
    response = {'data': {}}
    overview_res = ''
    overall_res = ''
    query_ip = ip or getattr(settings, 'WEBSOCKET_STATS_IP', '')
    if not query_ip:
        return {
            'site': site_config.get('sitename'),
            'status': 400,
            'error_msg': 'Set WEBSOCKET_STATS_IP in env or pass ip to get_websocket_update',
            'data': {'overview': '', 'overall': ''},
        }
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        result = nodeObj.websocketStats(ip=query_ip)
        if result['status'] == 200:
            overview_res = result['data']['overview']
            overall_res = result['data']['overall']

        response['site'] = site_config['sitename']
        response['status'] = result['status']
        response['data']['overview'] = overview_res
        response['data']['overall'] = overall_res
    except Exception as e:
        logger.exception("get_websocket_update failed for site=%s", site_config.get('sitename'))
        response['error_msg'] = f"overallchartdetails : Ex = {str(e)}"
        response['data']['overview'] = overview_res
        response['data']['overall'] = overall_res
        response['status'] = 400
    return response


# ---------------------------------------------------------------------------
# SNMP configuration
# ---------------------------------------------------------------------------

def save_snmp_config(data):
    """Create or update an SNMP configuration entry and regenerate YAML.

    Args:
        data (dict): Must include ``iplists``, ``version``, ``models``, and
            optional SNMPv3 fields (``user_name``, ``securitylevel``,
            ``Authentication``, ``Privacy``, ``auth_password``,
            ``privacy_password``, ``comm_string``, ``threshold``).

    Returns:
        dict: ``{'status': int, 'data': str}``
    """
    response = {}
    try:
        ip_list = data["iplists"]
        version = data["version"]
        models = data["models"]
        user_name = data.get("user_name", "NOVALUE")
        securitylevel = data.get("securitylevel", "NOVALUE")
        authentication = data.get("Authentication", "NOVALUE")
        privacy = data.get("Privacy", "NOVALUE")
        auth_password = data.get("auth_password", "NOVALUE")
        privacy_password = data.get("privacy_password", "NOVALUE")
        comm_string = data.get("comm_string", "NOVALUE")
        threshold = data.get("threshold")

        existing = SnmpModel.objects.filter(ipaddress=ip_list).first()
        if existing:
            existing.username = user_name
            existing.protocol = version
            existing.model = models
            existing.securitylevel = securitylevel
            existing.authenticationmethod = authentication
            existing.privacymethod = privacy
            existing.authenticationpassword = auth_password
            existing.privacypassword = privacy_password
            existing.communitystring = comm_string
            existing.threshold = threshold
            existing.save()
            msg = "Successfully table were edited!!"
        else:
            SnmpModel.objects.create(
                ipaddress=ip_list,
                protocol=version,
                username=user_name,
                model=models,
                securitylevel=securitylevel,
                authenticationmethod=authentication,
                privacymethod=privacy,
                authenticationpassword=auth_password,
                privacypassword=privacy_password,
                communitystring=comm_string,
                threshold=threshold,
            )
            msg = "Successfully table were created!!"

        cursor = connection.cursor()
        generate_snmp_yaml(cursor)

        response['status'] = 200
        response['data'] = msg
    except Exception as e:
        logger.exception("save_snmp_config failed")
        response['status'] = 400
        response['data'] = "Failure in creating table!! " + str(e)
    return response


def delete_snmp_config(row_id):
    """Delete an SNMP configuration row and regenerate the YAML.

    Returns:
        dict: ``{'status': int, 'msg': str, 'rowid': int}``
    """
    response = {}
    try:
        SnmpModel.objects.get(id=row_id).delete()
        cursor = connection.cursor()
        generate_snmp_yaml(cursor)
        response['status'] = 200
        response['msg'] = 'Successfully deleted snmp'
        response['rowid'] = row_id
    except Exception as e:
        logger.exception("delete_snmp_config failed for row_id=%s", row_id)
        response['status'] = 400
        response['data'] = "Failure in delete table!! " + str(e)
    return response


def get_all_snmp_configs():
    """Return all SNMP configurations as a list of dicts.

    Returns:
        dict: ``{'status': int, 'data': list[dict]}``
    """
    response = {}
    try:
        temp_list = []
        for entry in SnmpModel.objects.all():
            temp_list.append({
                "id": entry.id,
                "version": entry.protocol,
                "ipaddress": entry.ipaddress,
                "username": entry.username,
                "model": entry.model,
                "comm_string": entry.communitystring,
                "sec_level": entry.securitylevel,
                "auth_method": entry.authenticationmethod,
                "auth_password": entry.authenticationpassword,
                "priv_method": entry.privacymethod,
                "priv_password": entry.privacypassword,
                "snmp_threshold": entry.threshold,
            })
        response['status'] = 200
        response['data'] = temp_list
    except Exception as e:
        logger.exception("get_all_snmp_configs failed")
        response['status'] = 400
        response['msg'] = 'Something went wrong: ' + str(e)
    return response


def generate_snmp_yaml(cursor):
    """Regenerate ``./prom-conf/snmp.yaml`` from the database.

    Args:
        cursor: A Django database cursor.

    Returns:
        dict: The YAML data structure that was written.
    """
    ret = {"snmp": []}
    try:
        cursor.execute("SELECT ipaddress FROM snmp")
        ips = cursor.fetchall()
        for ip_row in ips:
            _ip = ip_row[0]
            sub = {_ip: []}
            values = SnmpModel.objects.filter(ipaddress=_ip).values()
            for k, v in values[0].items():
                if v == 'NOVALUE':
                    continue
                item = {}
                if k == 'threshold':
                    item[k] = []
                    thresholds = json.loads(v.replace("''", "\"").replace("'", "\""))
                    for tname, tval in thresholds.items():
                        item[k].append({tname: tval})
                else:
                    item[k] = v
                sub[_ip].append(item)
            ret["snmp"].append(sub)

        with open("./prom-conf/snmp.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("generate_snmp_yaml failed")
    return ret


def validate_snmp(data):
    """Validate SNMP connectivity with the provided parameters.

    Args:
        data (dict): SNMP connection parameters.

    Returns:
        dict: ``{'status': int, 'result': bool, 'data': str}``
    """
    response = {}
    try:
        result = snmp(
            version=data["version"],
            securitylevel=data.get("securitylevel", "NOVALUE"),
            username=data.get("user_name", "NOVALUE"),
            community_string=data.get("comm_string", "NOVALUE"),
            authenticationmethod=data.get("Authentication", "NOVALUE"),
            authenticationpassword=data.get("auth_password", "NOVALUE"),
            privacymethod=data.get("Privacy", "NOVALUE"),
            privacypassword=data.get("privacy_password", "NOVALUE"),
            ip=data["iplists"],
        ).check()

        response['status'] = 200
        response['result'] = result
        response['data'] = "Successfully validated!!" if result else "validation Failed!!"
    except Exception as e:
        logger.exception("validate_snmp failed")
        response['result'] = 'false'
        response['data'] = "Failure in validated!! " + str(e)
    return response


# ---------------------------------------------------------------------------
# Miscellaneous queries
# ---------------------------------------------------------------------------

def get_host_or_icon_nodes(site_config):
    """Fetch host and icon nodes for the dashboard topology view.

    Returns:
        dict: ``{'site': str, 'nodes_data': dict, 'code': int}``
    """
    temp_obj = {'site': site_config['sitename']}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        host_res = nodeObj._visallhost(mode='hosts')
        host_response = {'status': host_res['status']}
        if host_res['status'] == 200:
            host_response['data'] = host_res['data']

        icons_res = nodeObj._visicon(mode='icons')
        icons_response = {'status': icons_res['status']}
        if icons_res['status'] == 200:
            icons_response['data'] = icons_res['data']

        temp_obj['nodes_data'] = {"hosts": host_response, "icons": icons_response}
        temp_obj['code'] = 200
    except Exception as e:
        logger.exception("get_host_or_icon_nodes failed")
        temp_obj['nodes_data'] = {}
        temp_obj['code'] = 500
    return temp_obj


def get_chart_specific_details(site_config):
    """Return layer-wise count data for chart rendering.

    Returns:
        dict: ``{'status': int, 'data': dict}``
    """
    response = {}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        result = nodeObj.layerwiseCount()
        response['status'] = result['status']
        if result['status'] == 200:
            response['data'] = result
    except Exception as e:
        logger.exception("get_chart_specific_details failed")
        response['error_msg'] = f"chartspecificdetails : Ex = {str(e)}"
        response['status'] = 400
    return response


def get_status_all(site_config):
    """Fetch per-IP status summaries from Neo4j.

    Returns:
        dict: ``{'site': str, 'status_data': dict, 'code': int}``
    """
    temp_obj = {'site': site_config['sitename']}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        nic_res = nodeObj._getstatusAll()
        nic_response = {'status': nic_res['status']}
        if nic_res['status'] == 200:
            nic_response['data'] = {}
            for item in nic_res['data']:
                item = item[0]
                ip, values = item.split(':', 1)
                values = values.strip('[]').split(',')
                formatted = {}
                for pair in values:
                    key, value = pair.strip('{}').split(':')
                    formatted[key] = int(value)
                nic_response['data'][ip] = formatted

        temp_obj['status_data'] = {"Status_data": nic_response}
        temp_obj['code'] = 200
    except Exception as e:
        logger.exception("get_status_all failed")
        temp_obj['status_data'] = {}
        temp_obj['code'] = 500
    return temp_obj


def get_switches(site_config):
    """Fetch switch data from Neo4j.

    Returns:
        dict: ``{'site': str, 'response': dict, 'code': int}``
    """
    data = {'site': site_config['sitename']}
    try:
        nodeObj = Node(
            host=site_config['entity_host'],
            port=site_config['entity_port'],
            secure=site_config['is_URLSecured'],
        )
        switch_res = nodeObj._getswitchesAll()
        switch_response = {'status': switch_res['status']}
        if switch_res['status'] == 200:
            switch_response['data'] = [item[1] for item in switch_res['data']]

        data['response'] = switch_response
        data['code'] = 200
    except Exception as e:
        logger.exception("get_switches failed")
        data['response'] = {}
        data['code'] = 500
    return data

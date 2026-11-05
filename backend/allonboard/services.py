"""
Allonboard service layer (PREVENTIVE FIX #27).

Encapsulates business logic for device onboarding, YAML generation, Neo4j
node creation, email notifications, and device connectivity validation.
Functions accept plain Python data and return dicts -- never Django
request/response objects.
"""

import ast
import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yaml
from django.conf import settings
from django.db import connection

from lib.LinkedEyeEntity.Node import Node
from lib.LinkedEyeStruct.LinkedEyeStruct import K8S
from lib.LinkedEyeValidation import ilo, letelnet
from allonboard.models import allonboardModel, allmanagementModel
from dashboard.models import SnmpModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device onboarding (DB persistence)
# ---------------------------------------------------------------------------

def save_onboard_device(ip, host_data, selecthost, servertype, subipaddress,
                        emailid, textname, pathhost, phyipaddr,
                        mainipaddress, service_data=""):
    """Persist a single device record to ``allonboardModel``.

    Updates the existing row in place (same PK, same attached
    allmanagementModel rows) when one matches *ip*; creates a new row
    otherwise. Callers used to pass an ``is_edit`` flag that deleted the
    existing row first -- that destroyed the device's management credentials
    via the ``on_delete=CASCADE`` FK for no reason, since this function
    already knows how to update in place. There's no "edit" mode anymore:
    editing IS just calling this again with the same ip.

    Args:
        ip (str): Device IP address.
        host_data (dict): Host template data (serialised as JSON in DB).
        selecthost (str): Selected host template name.
        servertype (str): Application / environment type.
        subipaddress (str): Sub-IP address.
        emailid (str): Contact email.
        textname (str): Friendly name.
        pathhost (str): Path host category.
        phyipaddr (str): Physical IP address.
        mainipaddress (str): Main / primary IP address.
        service_data (str): Service name (optional).

    Returns:
        allonboardModel|None: The saved model instance, or ``None`` if
        an existing record was simply updated in-place.
    """
    try:
        existing = allonboardModel.objects.filter(ipaddress=ip).first()
        if existing:
            # Update in place -- unconditionally, not just when
            # textname/emailid/selecthost changed. Re-assigning identical
            # values is a harmless no-op when nothing actually changed.
            existing.selecthost = selecthost
            existing.servertype = servertype
            existing.subipaddress = subipaddress
            existing.emailid = emailid
            existing.servicename = service_data
            existing.json = json.dumps(host_data)
            existing.textname = textname
            existing.pathhost = pathhost
            existing.phyipaddr = phyipaddr
            existing.mainipaddress = mainipaddress
            existing.save()
            return None  # updated in-place
        else:
            obj = allonboardModel(
                hostname=ip,
                ipaddress=ip,
                servicename=service_data,
                selecthost=selecthost,
                subipaddress=subipaddress,
                servertype=servertype,
                emailid=emailid,
                textname=textname,
                pathhost=pathhost,
                phyipaddr=phyipaddr,
                mainipaddress=mainipaddress,
                json=json.dumps(host_data),
            )
            return obj
    except Exception as e:
        logger.exception("save_onboard_device failed for ip=%s", ip)
        raise


def save_management_entry(ip, mgmt_data, prototype):
    """Save or update a management entry (ILO, iDRAC, node-exporter, etc.).

    Args:
        ip (str): Device IP address.
        mgmt_data (dict): Management data including ``username``, ``password``,
            ``iloip``, ``port``, ``threshold``.
        prototype (str): Management type (``'ilo'``, ``'idrac'``,
            ``'Node Expo'``, ``'Window Expo'``, ``'Nginx Expo'``).
    """
    try:
        # allmanagementModel.save() (models.py) already updates the existing
        # (ipaddress, prototype) row in place if one exists -- no need to
        # delete it first.
        onboard_id = allonboardModel.objects.get(ipaddress=ip).id
        allmanagementModel(
            ip_id=onboard_id,
            ipaddress=ip,
            prototype=prototype,
            username=mgmt_data.get("username", ""),
            password=mgmt_data.get("password", ""),
            iloip=mgmt_data.get("iloip", ""),
            port=mgmt_data.get("port", ""),
            threshold=mgmt_data.get("threshold", ""),
        ).save()
    except Exception as e:
        logger.exception("save_management_entry failed for ip=%s prototype=%s", ip, prototype)
        raise


# ---------------------------------------------------------------------------
# Neo4j node creation
# ---------------------------------------------------------------------------

def create_device_nodes(json_output):
    """Create Neo4j nodes and relationships from YAML-derived host data.

    This mirrors the ``createnodes`` function from ``views.py``.

    Args:
        json_output (list[dict]): Each dict is a host/service descriptor
            containing ``_NEO4j_*`` keys.

    Returns:
        dict: ``{'status': int, 'data': str}``
    """
    client = Node()
    response = {}
    try:
        if not json_output:
            return {'status': 200, 'data': 'No nodes to create'}

        # Phase 1 -- create host/service nodes
        for hostObj in json_output:
            hostDic = {k: hostObj[k] for k in hostObj if k.startswith("_NEO4j_")}
            if hostDic:
                _struct = K8S("").convertor('nagios', hostDic)
                client.create(ast.literal_eval(_struct))

        # Phase 2 -- create relationships
        for serviceObj in json_output:
            serviceDic = {k: serviceObj[k] for k in serviceObj if k.startswith("_NEO4j_")}
            if not serviceDic:
                continue
            if serviceDic["_NEO4j_parent"] == '""' or not serviceDic["_NEO4j_title"]:
                continue
            if serviceObj.get("_NEO4J_RELATIONSHIP_RQD", "Yes") == 'No':
                response['status'] = 200
                response['data'] = "No relationship !!"
            else:
                createResponse = client.addRel(
                    serviceDic["_NEO4j_parent"],
                    serviceDic["_NEO4j_title"],
                )
                if createResponse['status'] == 200:
                    response['status'] = createResponse['status']
                    response['data'] = "Successfully nodes were created!!"
                else:
                    response['status'] = createResponse['status']
                    response['data'] = (
                        f"Failure in relationship creation "
                        f"{serviceDic['_NEO4j_parent']} - {serviceDic['_NEO4j_title']}"
                    )
        return response
    except Exception as e:
        logger.exception("create_device_nodes failed")
        return {'status': 400, 'data': "Failure in node creation!! " + str(e)}


# ---------------------------------------------------------------------------
# Device deletion
# ---------------------------------------------------------------------------

def delete_onboard_device(all_ips, username=None):
    """Delete device(s) from the DB and Neo4j.

    Args:
        all_ips (list[str]): IP addresses to delete.
        username (str|None): For audit-log purposes (caller handles logging).

    Returns:
        dict: ``{'status': int, 'data': str}``
    """
    response = {}
    try:
        for ip in all_ips:
            if not ip or not ip.strip():
                continue
            allonboardModel.objects.filter(ipaddress=ip).delete()
            SnmpModel.objects.filter(ipaddress=ip).delete()

            cursor = connection.cursor()
            _regenerate_all_yamls(cursor)

            client = Node()
            if client._check(ip, key='hostIp', resOut=True):
                client.execute(f"MATCH (a {{ hostIp:'{ip}' }}) DETACH DELETE a")
            else:
                logger.info("Node for ip=%s already deleted from Neo4j", ip)

        response['status'] = 200
        response['data'] = "Hosts deleted successfully"
    except Exception as e:
        logger.exception("delete_onboard_device failed")
        response['status'] = 400
        response['data'] = "Not able to delete hosts"
    return response


# ---------------------------------------------------------------------------
# YAML generation for monitoring exporters
# ---------------------------------------------------------------------------

def generate_monitoring_yaml(exporter_type, cursor):
    """Generate a YAML config file for the given exporter type.

    Args:
        exporter_type (str): One of ``'ping'``, ``'ilo'``, ``'idrac'``,
            ``'node'``, ``'windows'``, ``'nginx'``.
        cursor: Django database cursor.
    """
    generators = {
        'ping': _generate_ping_yaml,
        'ilo': _generate_ilo_yaml,
        'idrac': _generate_idrac_yaml,
        'node': _generate_node_yaml,
        'windows': _generate_windows_yaml,
        'nginx': _generate_nginx_yaml,
    }
    gen_func = generators.get(exporter_type)
    if gen_func:
        gen_func(cursor)
    else:
        logger.warning("Unknown exporter_type=%s", exporter_type)


def _generate_ping_yaml(cursor):
    """Regenerate ``./prom-conf/ping.yaml``."""
    ret = {"ping": []}
    try:
        cursor.execute("SELECT ipaddress FROM newonb")
        for ip_row in cursor.fetchall():
            ret["ping"].append(ip_row[0])
        with open("./prom-conf/ping.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("_generate_ping_yaml failed")


def _generate_ilo_yaml(cursor):
    """Regenerate ``./prom-conf/ilo.yaml``."""
    ret = {"ilo": []}
    try:
        cursor.execute("SELECT ipaddress FROM management where prototype='ilo'")
        for ip_row in cursor.fetchall():
            _ip = ip_row[0]
            sub = {_ip: []}
            values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='ilo').values()
            for k, v in values[0].items():
                if v == '':
                    continue
                sub[_ip].append({k: v})
            ret["ilo"].append(sub)
        with open("./prom-conf/ilo.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("_generate_ilo_yaml failed")


def _generate_idrac_yaml(cursor):
    """Regenerate ``./prom-conf/idrac.yaml``."""
    ret = {"idrac": []}
    try:
        cursor.execute("SELECT ipaddress FROM management where prototype='idrac'")
        for ip_row in cursor.fetchall():
            _ip = ip_row[0]
            sub = {_ip: []}
            values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='idrac').values()
            for k, v in values[0].items():
                if v == '':
                    continue
                sub[_ip].append({k: v})
            ret["idrac"].append(sub)
        with open("./prom-conf/idrac.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("_generate_idrac_yaml failed")


def _generate_node_yaml(cursor):
    """Regenerate ``./prom-conf/node.yaml``."""
    ret = {"node_exporter": []}
    try:
        cursor.execute("SELECT ipaddress FROM management where prototype = 'Node Expo'")
        for ip_row in cursor.fetchall():
            _ip = ip_row[0]
            sub = {_ip: []}
            values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='Node Expo').values()
            for k, v in values[0].items():
                if v == '':
                    continue
                if k == 'threshold':
                    item = {k: []}
                    thresholds = json.loads(v.replace("''", "\"").replace("'", "\""))
                    for tname, tval in thresholds.items():
                        item[k].append({tname: tval})
                else:
                    item = {k: v}
                sub[_ip].append(item)
            ret["node_exporter"].append(sub)
        with open("./prom-conf/node.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("_generate_node_yaml failed")


def _generate_windows_yaml(cursor):
    """Regenerate ``./prom-conf/windows.yaml``."""
    ret = {"windows_exporter": []}
    try:
        cursor.execute("SELECT ipaddress FROM management where prototype = 'Window Expo'")
        for ip_row in cursor.fetchall():
            _ip = ip_row[0]
            sub = {_ip: []}
            values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='Window Expo').values()
            for k, v in values[0].items():
                if v == '':
                    continue
                if k == 'threshold':
                    item = {k: []}
                    thresholds = json.loads(v.replace("''", "\"").replace("'", "\""))
                    for tname, tval in thresholds.items():
                        item[k].append({tname: tval})
                else:
                    item = {k: v}
                sub[_ip].append(item)
            ret["windows_exporter"].append(sub)
        with open("./prom-conf/windows.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("_generate_windows_yaml failed")


def _generate_nginx_yaml(cursor):
    """Regenerate ``./prom-conf/nginx.yaml``."""
    ret = {"nginx_exporter": []}
    try:
        cursor.execute("SELECT ipaddress FROM management where prototype = 'Nginx Expo'")
        for ip_row in cursor.fetchall():
            _ip = ip_row[0]
            sub = {_ip: []}
            values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='Nginx Expo').values()
            for k, v in values[0].items():
                if v == '':
                    continue
                if k == 'threshold':
                    item = {k: []}
                    thresholds = json.loads(v.replace("''", "\"").replace("'", "\""))
                    for tname, tval in thresholds.items():
                        item[k].append({tname: tval})
                else:
                    item = {k: v}
                sub[_ip].append(item)
            ret["nginx_exporter"].append(sub)
        with open("./prom-conf/nginx.yaml", "w+") as f:
            yaml.safe_dump(ret, f, default_flow_style=False)
    except Exception as e:
        logger.exception("_generate_nginx_yaml failed")


def _regenerate_all_yamls(cursor):
    """Regenerate all monitoring YAML files (called during device deletion)."""
    _generate_ping_yaml(cursor)
    _generate_ilo_yaml(cursor)
    _generate_idrac_yaml(cursor)
    _generate_node_yaml(cursor)
    _generate_windows_yaml(cursor)
    _generate_nginx_yaml(cursor)


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def send_onboard_notification(emailid, message, device_data, sitename):
    # Non-blocking: SMTP egress is unavailable in the 443-only network, so run the
    # notification send in a background daemon thread. Onboarding must never block
    # or 504 on email delivery. Delivery is best-effort and still works if SMTP
    # becomes reachable.
    import threading
    threading.Thread(target=_impl_send_onboard_notification, args=(emailid, message, device_data, sitename,), daemon=True).start()
    return True

def _impl_send_onboard_notification(emailid, message, device_data, sitename):

    """Send an onboarding notification email with device details table.

    Args:
        emailid (str): Primary recipient email address.
        message (str): Action description (e.g. ``"Onboarded Successfully"``).
        device_data (list[dict]): Each dict describes a device with keys like
            ``selecthost``, ``ipaddress``, ``servertype``, etc.
        sitename (str): Site name for the email subject.

    Returns:
        bool: ``True`` if sent successfully, ``False`` otherwise.
    """
    try:
        smtp_server = "smtp.office365.com"
        smtp_port = 587
        smtp_user = settings.LINKEDEYE_EMAIL
        smtp_pass = settings.LINKEDEYE_EMAIL_APPKEY
        cc_list = ["devops@finspot.in"]

        msg = MIMEMultipart("alternative")
        msg['From'] = smtp_user
        msg['To'] = emailid
        msg['Cc'] = ', '.join(cc_list)
        msg['Subject'] = sitename + " - Onboarding Device Report"

        table_headers = """
            <tr>
                <th>Selecthost</th><th>IPaddress</th><th>SubIPaddress</th>
                <th>Environment</th><th>EmailID</th><th>Service Name</th>
                <th>Text Name</th><th>Path Host</th><th>Physical IP</th>
            </tr>
        """
        table_rows = ""
        for data in device_data:
            cleaned = data.get('selecthost', '').split('-')[0].replace('.yaml', '')
            table_rows += f"""
                <tr>
                    <td>{cleaned}</td>
                    <td>{data.get('ipaddress', '')}</td>
                    <td>{data.get('subipaddress', '')}</td>
                    <td>{data.get('servertype', '')}</td>
                    <td>{data.get('emailid', '')}</td>
                    <td>{data.get('servicename', '')}</td>
                    <td>{data.get('textname', '')}</td>
                    <td>{data.get('pathhost', '')}</td>
                    <td>{data.get('phyipaddr', '')}</td>
                </tr>
            """

        html_body = f"""
        <html><body>
            <p>The following devices have been {message}:</p>
            <table border="1" cellpadding="5" cellspacing="0">
                {table_headers}{table_rows}
            </table>
        </body></html>
        """

        msg.attach(MIMEText(html_body, "html"))
        all_recipients = [emailid] + cc_list

        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, all_recipients, msg.as_string())
        server.quit()

        logger.info("Onboard email sent to %s with CC %s", emailid, cc_list)
        return True
    except Exception as e:
        logger.exception("send_onboard_notification failed")
        return False


# ---------------------------------------------------------------------------
# Device connection validation
# ---------------------------------------------------------------------------

def validate_device_connection(device_type, ip, port=None, username=None,
                               password=None, iloip=None):
    """Validate connectivity to a device (ILO, iDRAC, node-exporter, etc.).

    Args:
        device_type (str): One of ``'ilo'``, ``'idrac'``, ``'node_expo'``,
            ``'nginx_expo'``, ``'win_expo'``.
        ip (str): Target IP address.
        port (str|int|None): Target port (for telnet-based checks).
        username (str|None): Credentials username (ILO only).
        password (str|None): Credentials password (ILO only).
        iloip (str|None): ILO management IP (ILO only).

    Returns:
        dict: ``{'status': 200, 'result': bool, 'data': str}``
    """
    response = {}
    try:
        if device_type == 'ilo':
            result = ilo(ip=iloip or ip, username=username, password=password).check()
        else:
            # idrac, node_expo, nginx_expo, win_expo all use telnet check
            result = letelnet(ip=ip, port=port).check()

        response['status'] = 200
        response['result'] = result
        response['data'] = "Successfully validated!!" if result else "validation Failed!!"
    except Exception as e:
        logger.exception("validate_device_connection failed for type=%s ip=%s", device_type, ip)
        response['result'] = 'false'
        response['data'] = "Failure in validated!! " + str(e)
    return response

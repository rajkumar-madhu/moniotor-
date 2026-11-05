"""
Notification service layer (PREVENTIVE FIX #27).

Encapsulates business logic for Apprise-based notifications, notification
settings CRUD, escalation policy management, email toggling, and snooze
logic.  Functions accept plain Python data and return dicts -- never Django
request/response objects.
"""

import datetime
import json
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.forms.models import model_to_dict

from auditlogs.models import AuditlogsModel
from lib.LinkedEyeEntity import Node
from lib.LinkedEyeNotification import Notification
from notification.models import ServiceModel, UserNotificationSetingsModel
from userprofile.models import policynotifiModel
from userprofile.policy_schema import parse_policy_timer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------

def get_notification_settings(username):
    """Return all notification services and the user's saved preferences.

    Args:
        username (str): The Django ``User.username``.

    Returns:
        dict: ``{'status': int, 'data': list[dict], 'userobj': dict}`` on
        success, or error payload on failure.
    """
    response = {}
    try:
        user = User.objects.get(username=username)
        user_dict = model_to_dict(user, fields=[f.name for f in user._meta.fields])

        # Previous successful login
        last_logins = AuditlogsModel.objects.filter(
            username=str(username), action='User login', status='Success'
        ).order_by('-created')
        if last_logins.count() > 1:
            user_dict['last_login'] = last_logins[1].created
        else:
            user_dict['last_login'] = user.last_login

        # Ensure default mail service exists
        _ensure_default_mail_service()

        temp_list = []
        for svc in ServiceModel.objects.all():
            temp_list.append({
                "id": svc.id,
                "name": svc.name,
                "syntax": svc.syntax,
                "delimiter": svc.delimiter,
                "is_defaultservice": svc.is_defaultservice,
                "messageformat": svc.messageformat,
            })

        response['status'] = 200
        response['data'] = temp_list
        response['userobj'] = user_dict
    except Exception as e:
        logger.exception("get_notification_settings failed for user=%s", username)
        response['status'] = 400
        response['msg'] = 'Something went wrong'
    return response


def save_notification_settings(username, settings_data):
    """Persist the user's notification service selections and inputs.

    Args:
        username (str): Django username.
        settings_data (list[dict]): Each dict has ``serviceid`` and optional
            ``inputs`` mapping.

    Returns:
        dict: ``{'status': int, 'msg': str}``
    """
    response = {}
    try:
        user_id = User.objects.get(username=username).id
        UserNotificationSetingsModel.objects.filter(user_id=user_id).delete()

        selected_ids = []
        for data_obj in settings_data:
            selected_ids.append(data_obj["serviceid"])
            service = ServiceModel.objects.get(id=data_obj["serviceid"])

            if service.is_inputRequired:
                inputs = data_obj["inputs"]
                url = service.syntax
                for key in inputs:
                    url = url.replace('{' + key + '}', inputs[key])
            else:
                url = service.syntax

            existing = UserNotificationSetingsModel.objects.filter(
                user_id=user_id, service_id=data_obj["serviceid"]
            )
            if existing.exists():
                obj = existing.get(is_saved=True)
                obj.inputs = json.dumps(data_obj["inputs"])
                obj.url = url
                obj.save()
            else:
                UserNotificationSetingsModel.objects.create(
                    user_id=user_id,
                    service_id=data_obj["serviceid"],
                    inputs=json.dumps(data_obj["inputs"]),
                    url=url,
                    is_saved=True,
                )

        UserNotificationSetingsModel.objects.filter(
            user_id=user_id
        ).exclude(service_id__in=selected_ids).delete()

        response['status'] = 200
        response['msg'] = 'Successfully saved notification settings'

        AuditlogsModel(
            username=username,
            action='Save Notification Preferences',
            status='Success',
            message='Successfully saved notification settings',
        ).save()

    except Exception as e:
        logger.exception("save_notification_settings failed for user=%s", username)
        AuditlogsModel(
            username=username,
            action='Save Notification Preferences',
            status='Failure',
            message='Not able to save notification settings',
        ).save()
        response['status'] = 400
        response['msg'] = 'Something went wrong'
    return response


# ---------------------------------------------------------------------------
# Escalation policies
# ---------------------------------------------------------------------------

def create_escalation_policy(policy_data, username):
    """Create or update an escalation notification policy.

    Args:
        policy_data (dict): Keys include ``categories``, ``escalation_mails``,
            ``info_mails``, ``escalation_required``, ``approval_time``,
            ``resolution_time``, and optionally ``policyid`` for updates.
        username (str): For audit logging.

    Returns:
        dict: ``{'status': int, 'msg': str, 'data': dict}``
    """
    try:
        categories = policy_data.get('categories', '')
        policy_id = policy_data.get('policyid')

        if policy_id:
            policy = policynotifiModel.objects.get(policy_id=policy_id)
            policy.subject_category = policy_data.get('subject_category', '')
            policy.categories = categories
            policy.escalation_mails = json.dumps(policy_data.get('escalation_mails', []))
            policy.definite_mails = json.dumps(policy_data.get('info_mails', []))
            policy.escalation_required = bool(int(policy_data.get('escalation_required', 1)))
            policy.approval_timer = parse_policy_timer(policy_data.get('approval_time'))
            policy.resolution_timer = parse_policy_timer(policy_data.get('resolution_time'))
            policy.save()

            AuditlogsModel(
                username=username,
                action='Update Escalation Mail',
                status='Success',
                message=f'Escalation Mail details updated for category: {categories}',
            ).save()

            policy_data['policyid'] = policy.policy_id
            return {'status': 200, 'msg': 'Policy updated successfully', 'data': policy_data}
        else:
            policy = policynotifiModel.objects.create(
                subject_category=policy_data.get('subject_category', ''),
                categories=categories,
                escalation_mails=json.dumps(policy_data.get('escalation_mails', [])),
                definite_mails=json.dumps(policy_data.get('info_mails', [])),
                escalation_required=bool(int(policy_data.get('escalation_required', 1))),
                approval_timer=parse_policy_timer(policy_data.get('approval_time')),
                resolution_timer=parse_policy_timer(policy_data.get('resolution_time')),
            )

            AuditlogsModel(
                username=username,
                action='Create Escalation Mail',
                status='Success',
                message=f'New Escalation Mail details created for category: {categories}',
            ).save()

            policy_data['policyid'] = policy.policy_id
            return {'status': 200, 'msg': 'Policy created successfully', 'data': policy_data}

    except Exception as e:
        logger.exception("create_escalation_policy failed")
        AuditlogsModel(
            username=username,
            action='Create/Update Escalation Mail',
            status='Failed',
            message=f'Error occurred: {str(e)}',
        ).save()
        return {'status': 500, 'msg': f'Error: {str(e)}'}


def get_escalation_policies():
    """Return all escalation policies as a list of dicts.

    Returns:
        dict: ``{'status': 200, 'data': list[dict]}``
    """
    data = []
    for policy in policynotifiModel.objects.all():
        data.append({
            "policy_id": policy.policy_id,
            "subject_category": policy.subject_category,
            "escalation_mails": policy.escalation_mails,
            "info_mails": policy.definite_mails,
            "categories": policy.categories,
            "escalation_required": "Disabled" if policy.escalation_required == 0 else "Enabled",
            "approval_time": policy.approval_timer,
            "resolution_time": policy.resolution_timer,
            "device_type": policy.device_type,
            "device_ip": policy.device_ip,
            "device_friendly_name": policy.device_friendly_name,
        })
    return {"status": 200, "data": data}


def get_escalation_policy_detail(policy_id):
    """Return a single escalation policy by its ID.

    Returns:
        dict: ``{'status': int, 'data': dict}`` or error payload.
    """
    try:
        policy = policynotifiModel.objects.get(policy_id=policy_id)
        return {
            "status": 200,
            "data": {
                "policy_id": policy.policy_id,
                "subject_category": policy.subject_category,
                "device_type": policy.device_type,
                "device_ip": policy.device_ip,
                "device_friendly_name": policy.device_friendly_name,
                "escalation_mails": policy.escalation_mails,
                "info_mails": policy.definite_mails,
                "categories": policy.categories,
                "escalation_required": policy.escalation_required,
                "approval_time": policy.approval_timer,
                "resolution_time": policy.resolution_timer,
            },
        }
    except policynotifiModel.DoesNotExist:
        return {"status": 404, "msg": "Policy not found"}


def delete_escalation_policy(policy_id, username):
    """Delete an escalation policy by ID.

    Returns:
        dict: ``{'status': int, 'msg': str}``
    """
    try:
        policy = policynotifiModel.objects.filter(policy_id=policy_id).first()
        if policy:
            category = policy.categories
            policy.delete()
            AuditlogsModel(
                username=username,
                action='Delete Escalation Mail',
                status='Success',
                message=f'Escalation Mail details deleted for category: {category}',
            ).save()
            return {'status': 200, 'msg': 'Policy deleted successfully'}
        else:
            return {'status': 404, 'msg': 'Policy not found'}
    except Exception as e:
        logger.exception("delete_escalation_policy failed for policy_id=%s", policy_id)
        AuditlogsModel(
            username=username,
            action='Delete Escalation Mail',
            status='Failed',
            message=f'Error occurred while deleting Escalation Mail details: {str(e)}',
        ).save()
        return {'status': 500, 'msg': str(e)}


# ---------------------------------------------------------------------------
# Snooze / toggle helpers
# ---------------------------------------------------------------------------

def get_snooze_info(event_title):
    """Fetch the current email_notify status for an event from the DB.

    Args:
        event_title (str): The event title (``notification_system.events.title``).

    Returns:
        dict: ``{'status': int, 'current_status': int}`` or error payload.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT email_notify FROM notification_system.events WHERE title = %s",
                [event_title],
            )
            row = cursor.fetchone()
            if row:
                return {'status': 200, 'current_status': row[0]}
            else:
                return {'status': 404, 'msg': 'Event not found'}
    except Exception as e:
        logger.exception("get_snooze_info failed for title=%s", event_title)
        return {'status': 500, 'msg': str(e)}


def toggle_email_notification(event_title, status_val, username=None):
    """Set the email_notify flag for an event.

    Args:
        event_title (str): Event title.
        status_val (int): 1 = resumed, 0 = paused.
        username (str|None): For audit logging.

    Returns:
        dict: ``{'status': int, 'msg': str}``
    """
    try:
        if status_val not in (0, 1):
            return {'status': 400, 'msg': 'Invalid status value. Must be 0 or 1.'}

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM notification_system.events WHERE title = %s",
                [event_title],
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                cursor.execute(
                    "UPDATE notification_system.events SET email_notify = %s WHERE title = %s",
                    [status_val, event_title],
                )
                msg = (
                    "Email notifications resumed"
                    if status_val == 1
                    else "Email notifications paused"
                )
            else:
                return {
                    'status': 404,
                    'msg': f'Event with title "{event_title}" not found in notification_system.events',
                }

        AuditlogsModel(
            username=username,
            action='Toggle Email Notification',
            status='Success',
            message=f'{msg} for {event_title}',
        ).save()

        return {'status': 200, 'msg': msg}
    except Exception as e:
        logger.exception("toggle_email_notification failed for title=%s", event_title)
        return {'status': 500, 'msg': str(e)}


# ---------------------------------------------------------------------------
# Send notification via Apprise
# ---------------------------------------------------------------------------

def send_notification(title, body, urls):
    """Dispatch a notification via the Apprise gateway.

    Args:
        title (str): Notification title.
        body (str): Notification body / message.
        urls (str): Apprise-compatible URL(s).

    Returns:
        dict: ``{'status': int, 'msg': str}``
    """
    try:
        payload = {"urls": urls, "title": title, "body": body}
        r = requests.post(
            f'http://{settings.APPRISE_HOST}/notify/',
            json=payload,
            timeout=30,
        )
        return {'status': r.status_code, 'msg': 'Notification sent'}
    except Exception as e:
        logger.exception("send_notification failed")
        return {'status': 500, 'msg': str(e)}


def send_notifications_for_user(username, title, template_type, data_obj):
    """Send notifications to all configured services for a given user.

    Args:
        username (str): Target user.
        title (str): Notification title.
        template_type (str): Template type for rendering.
        data_obj (dict): Variables to pass to the notification template.

    Returns:
        dict: ``{'status': int, 'msg': str}``
    """
    response = {}
    try:
        user_id = User.objects.get(username=username).id
        services = UserNotificationSetingsModel.objects.filter(user_id=user_id)
        notification_obj = Notification()

        for service in services:
            apobj = notification_obj.instantiate()
            apobj.add_url(service.url)
            message_format = ServiceModel.objects.get(id=service.service_id).messageformat
            result = apobj.sendnotifications(
                title=title,
                message_format=message_format,
                template_type=template_type,
                variables=data_obj,
            )
            if result['data'] is False:
                AuditlogsModel(
                    username=username,
                    action='Send Notification',
                    status='Failure',
                    message=result['error_msg'],
                ).save()

        response['status'] = 200
        response['msg'] = ' '
    except Exception as e:
        logger.exception("send_notifications_for_user failed for user=%s", username)
        response['status'] = 200
        response['msg'] = str(e)
    return response


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_default_mail_service():
    """Create the default ``mail`` service if it does not exist."""
    if ServiceModel.objects.filter(name='mail').exists():
        return

    try:
        linkedeye_email = settings.LINKEDEYE_EMAIL.split("@")
        username_part = linkedeye_email[0]
        domain = linkedeye_email[1]

        if 'gmail.com' in domain.lower():
            syntax = (
                'mailto://' + username_part + ':' + settings.LINKEDEYE_EMAIL_APPKEY
                + '@' + domain + '/{email}'
            )
        else:
            if 'outlook.com' in domain or 'hotmail.com' in domain:
                smtp_server = 'smtp.office365.com'
            elif 'yahoo.com' in domain:
                smtp_server = 'smtp.mail.yahoo.com'
            else:
                smtp_server = 'smtp.office365.com'

            syntax = (
                'mailtos://' + domain + '?smtp=' + smtp_server
                + '&user=' + settings.LINKEDEYE_EMAIL
                + '&pass=' + settings.LINKEDEYE_EMAIL_APPKEY
                + '&to={email}'
            )

        ServiceModel.objects.create(
            name='mail',
            syntax=syntax,
            delimiter=',',
            is_inputRequired=True,
            is_defaultservice=True,
            messageformat='Html',
        )
    except Exception as e:
        logger.exception("_ensure_default_mail_service failed")


def convert_timestamp(item_date_object):
    """Convert datetime objects to Unix timestamp (for JSON serialization)."""
    if isinstance(item_date_object, (datetime.date, datetime.datetime)):
        return item_date_object.timestamp()

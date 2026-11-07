"""
Celery Tasks for Notification Module
CRITICAL FIX #5: Replace threading.Timer with Celery delayed tasks
"""

from celery import shared_task
from django.db import connection
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def resume_email_notifications(self, event_title):
    """
    Resume email notifications after snooze period expires.
    CRITICAL FIX #5: Replaces threading.Timer with Celery apply_async(countdown=N)
    
    Benefits:
    - No extra threads (~8MB stack each)
    - Uses existing RabbitMQ worker pool
    - Proper retry logic
    - Persistent across restarts
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE notification_system.events SET email_notify = 1 WHERE title = %s",
                [event_title]
            )
        logger.info(f"Snooze expired: Notifications resumed for {event_title}")
        return {'status': 'success', 'event': event_title}
    except Exception as exc:
        logger.warning(f"Failed to resume notifications for {event_title}: {exc}")
        # Retry with exponential backoff
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_snooze_email(self, to_emails, cc_emails, subject, html_content):
    """
    Send snooze notification email via Celery.
    Offloads email sending from request thread.
    CRITICAL FIX #19: Use environment variables for SMTP credentials.
    """
    import smtplib
    import ssl
    import os
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from django.conf import settings
    
    try:
        smtp_server = os.getenv('SMTP_SERVER', 'smtp.office365.com')
        smtp_port = int(os.getenv('SMTP_PORT', '587'))
        smtp_user = os.getenv('SMTP_USER', settings.LINKEDEYE_EMAIL)
        smtp_pass = settings.LINKEDEYE_EMAIL_APPKEY or os.getenv('SMTP_PASSWORD', '')
        
        msg = MIMEMultipart()
        msg['From'] = f"Eva <{smtp_user}>"
        msg['To'] = ", ".join(to_emails)
        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)
        msg['Subject'] = subject
        msg.attach(MIMEText(html_content, 'html'))
        
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            recipients = to_emails + cc_emails
            server.sendmail(smtp_user, recipients, msg.as_string())
        
        logger.info(f"Snooze email sent successfully to {to_emails}")
        return {'status': 'success', 'recipients': len(recipients)}
    except Exception as exc:
        logger.error(f"SMTP Error sending snooze email: {exc}")
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_notification_via_apprise(self, url, title, body, message_format='Html'):
    """
    Send notification via Apprise using Celery.
    Offloads notification sending from request thread.
    """
    from lib.LinkedEyeNotification import Notification
    
    try:
        notification_obj = Notification()
        notification_obj.add_url(url)
        result = notification_obj.sendnotifications(
            title=title,
            message_format=message_format,
            message_body=body
        )
        
        if result['data']:
            logger.info(f"Notification sent: {title}")
            return {'status': 'success'}
        else:
            logger.error(f"Notification failed: {result['error_msg']}")
            raise Exception(result['error_msg'])
    except Exception as exc:
        logger.error(f"Failed to send notification: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))

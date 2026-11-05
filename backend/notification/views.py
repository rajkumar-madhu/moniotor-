from django.shortcuts import render,HttpResponse
from django.views.decorators.csrf import csrf_exempt
import json
import requests
from django.conf import settings
from django.db import connection
from .models import ServiceModel, UserNotificationSetingsModel
from django.contrib.auth.models import User
from django.forms.models import model_to_dict
import datetime
from django.template.loader import render_to_string
import html2markdown
import html2text
from auditlogs.models import AuditlogsModel
from userprofile.models import policynotifiModel
from userprofile.policy_schema import parse_policy_timer
from allonboard.models import allonboardModel
from django.http import JsonResponse
import os
from lib.LinkedEyeNotification import Notification
from lib.LinkedEyeEntity import Node
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Create your views here.
def index(request):
    return render(request, 'app/notificationsettings.html')

def serviceoperation(request):
    if request.method == 'POST':
        response = { }
        clientData = json.loads(request.POST['alldata'])
        parsed_json = clientData['data']
        try:
            if parsed_json["operation"] == 'add':
                if ServiceModel.objects.filter(name=parsed_json["name"]).exists():
                    response['msg'] = 'Service name already exist.'
                    response['status'] = 500
                    return HttpResponse(json.dumps(response), content_type="json")
                else:
                    obj = ServiceModel(name=parsed_json["name"],syntax = parsed_json["syntax"],delimiter=parsed_json["delimiter"],is_inputRequired=parsed_json["is_inputRequired"], is_defaultservice=parsed_json["is_defaultservice"], messageformat=parsed_json["messageformat"])
                    obj.save()
                    response['status'] = 200   
                    response['msg'] = 'Successfully saved service.'
                    response['servicename'] = parsed_json["name"] 
                    objid = ServiceModel.objects.get(name=parsed_json["name"]).id
                    response['id'] = objid
                    user_ids = list(User.objects.values_list('id', flat=True))
                    if parsed_json["is_inputRequired"]:
                        is_saved = False
                    else:
                        is_saved = True
                    for id in user_ids:
                        obj = UserNotificationSetingsModel(user_id=id, service_id = objid, inputs={}, url = parsed_json["syntax"], is_saved = is_saved)
                        obj.save()
            elif parsed_json["operation"] == 'delete':
                deleteobj = ServiceModel.objects.get(id=parsed_json["id"])
                deleteobj.delete()
                response['status'] = 200
                response['msg'] = 'Successfully deleted service.'
                response['id'] = parsed_json["id"]
            elif parsed_json["operation"] == 'update':
                obj = ServiceModel.objects.get(id=parsed_json["id"])
                obj.syntax = parsed_json["syntax"]
                obj.delimiter = parsed_json["delimiter"]
                obj.save()
                response['id'] = parsed_json["id"]
                response['status'] = 200
                response['msg'] = 'Successfully updated service.'
            return HttpResponse(json.dumps(response), content_type="json")
        except Exception as e:
            print('===Exception=====serviceoperation===')
            print(str(e))
            response['status'] = 400
            response['msg'] = 'Something went wrong'
    return HttpResponse(json.dumps(response), content_type="json")

def sendnotification(request):
    if request.method == 'POST':
        response = { }
        clientData = json.loads(request.POST['alldata'])
        parsed_json = clientData['data']
        email = request.user
        user_id = User.objects.get(username=email).id
        service = ServiceModel.objects.get(name = parsed_json["servicename"])
        if service.is_inputRequired:
            i = UserNotificationSetingsModel.objects.get(service_id = service.id, user_id = user_id).inputs
            inputs = json.loads(i)
            url = service.syntax
            for i in inputs:
                tmp = '{'+i+'}'
                url = url.replace(tmp,inputs[i])
        else:
            url = service.syntax
        payload = {
                    "urls": url,
                    "title": parsed_json["title"],
                    "body": parsed_json["body"]
                }
        r = requests.post('http://'+settings.APPRISE_HOST+"/notify/", json=payload)
    return HttpResponse(json.dumps(response), content_type="json")
def sendnotifications(request):
    try:
        response = { }
        clientData = json.loads(request.POST['alldata'])
        parsed_json = clientData['data']
        user_id = User.objects.get(username=parsed_json["user"]).id
        services = UserNotificationSetingsModel.objects.filter(user_id = user_id)
        notification_obj = Notification()
        for service in services:
            apobj = notification_obj.instantiate()
            url = service.url
            apobj.add_url(url)
            message_format = ServiceModel.objects.get(id = service.service_id).messageformat
            result = apobj.sendnotifications(title=parsed_json["title"], message_format=message_format, template_type=parsed_json["template_type"], variables=parsed_json["dataObj"])
            if result['data'] == False:
                log = AuditlogsModel(username = parsed_json["user"],  action = 'Send Notification', status = 'Failure', message=result['error_msg'])
                log.save()
        response['status'] = 200
        response['msg'] = ' '
    except Exception as e:
        response['status'] = 200
        response['msg'] = str(e)
        print('=Exception==sendnotifications==')
        print(str(e))
    return HttpResponse(json.dumps(response), content_type="json")
    
def get_all_services(request):
    response = {}
    userobj = User.objects.get(username=request.user)
    obj = model_to_dict(userobj, fields=[field.name for field in userobj._meta.fields])
    try:
        # Get the previous successful login from audit logs (second latest entry)
        last_logins = AuditlogsModel.objects.filter(username=str(request.user), action='User login', status='Success').order_by('-created')
        if last_logins.count() > 1:
            obj['last_login'] = last_logins[1].created
        else:
            obj['last_login'] = userobj.last_login
            
        temp_list = []
        service_object = ServiceModel.objects.filter(name='mail')
        """if not service_object:
            linkedeye_email = (settings.LINKEDEYE_EMAIL).split("@")
            syntax = 'mailto://'+linkedeye_email[0]+':'+settings.LINKEDEYE_EMAIL_APPKEY+"@"+linkedeye_email[1]+'/{email}'
            delimiter = '/'
            obj = ServiceModel(name='mail',syntax = syntax, delimiter=',',is_inputRequired=True, is_defaultservice=True, messageformat='Html')
            obj.save()"""
        if not service_object:
            linkedeye_email = (settings.LINKEDEYE_EMAIL).split("@")
            username = linkedeye_email[0]
            domain = linkedeye_email[1]
            
            # Check if Gmail or custom domain
            if 'gmail.com' in domain.lower():
                # Gmail: use simple mailto:// format
                syntax = 'mailto://' + username + ':' + settings.LINKEDEYE_EMAIL_APPKEY + '@' + domain + '/{email}'
            else:
                # Custom domain: use mailtos:// with SMTP server
                if 'outlook.com' in domain or 'hotmail.com' in domain:
                    smtp_server = 'smtp.office365.com'
                elif 'yahoo.com' in domain:
                    smtp_server = 'smtp.mail.yahoo.com'
                else:
                    smtp_server = 'smtp.office365.com'
                
                syntax = 'mailtos://' + domain + '?smtp=' + smtp_server + '&user=' + settings.LINKEDEYE_EMAIL + '&pass=' + settings.LINKEDEYE_EMAIL_APPKEY + '&to={email}'
            
            delimiter = '/'
            obj = ServiceModel(name='mail', syntax=syntax, delimiter=',', is_inputRequired=True, is_defaultservice=True, messageformat='Html')
            obj.save()
        app_obj = ServiceModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["name"] = temp.name
            json_obj["syntax"] = temp.syntax
            json_obj["delimiter"] = temp.delimiter
            json_obj["is_defaultservice"] = temp.is_defaultservice
            json_obj["messageformat"] = temp.messageformat
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        response['userobj'] = obj
        return HttpResponse(json.dumps(response, default=convert_timestamp))
    except Exception as e:
        print('=======Exception======')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))
# helper function to convert the datetime object into seconds 
def convert_timestamp(item_date_object):
    if isinstance(item_date_object, (datetime.date, datetime.datetime)):
        return item_date_object.timestamp()
def save_settings(request):
    if request.method == 'POST':
        response = { }
        try:
            data = request.POST['alldata']
            parsed_json = json.loads(data)
            email = request.user
            user_id = User.objects.get(username=email).id
            UserNotificationSetingsModel.objects.filter(user_id=user_id).delete()
            userselected_serviceids = []
            for data_obj in parsed_json['data']:
                userselected_serviceids.append(data_obj["serviceid"])
                service = ServiceModel.objects.get(id = data_obj["serviceid"])
                if service.is_inputRequired:
                    inputs = data_obj["inputs"]
                    url = service.syntax
                    for i in inputs:
                        tmp = '{'+i+'}'
                        url = url.replace(tmp,inputs[i])
                else:
                    url = service.syntax
                if UserNotificationSetingsModel.objects.filter(user_id=user_id,service_id = data_obj["serviceid"]).exists():
                    obj = UserNotificationSetingsModel.objects.get(user_id=user_id,service_id = data_obj["serviceid"], is_saved=True)
                    obj.inputs = str(json.dumps(data_obj["inputs"]))
                    obj.url = url
                    obj.save()
                else:  
                    obj = UserNotificationSetingsModel(user_id=user_id,service_id = data_obj["serviceid"],inputs=str(json.dumps(data_obj["inputs"])),url = url, is_saved=True)
                    obj.save()
            UserNotificationSetingsModel.objects.filter(user_id=user_id).exclude(service_id__in=userselected_serviceids).delete()
            response['status'] = 200
            response['msg'] = 'Successfully saved notification settings'
            log = AuditlogsModel(username = request.user,  action = 'Save Notification Preferences', status = 'Success', message='Successfully saved notification settings')
            log.save()
        except Exception as e:
            print('====Exception===')
            print(str(e))
            log = AuditlogsModel(username = request.user,  action = 'Save Notification Preferences', status = 'Failure', message='Not able to save notification settings')
            log.save()
            response['status'] = 400
            response['msg'] = 'Something went wrong'
    return HttpResponse(json.dumps(response))
def formatmessage(message, message_format):
    try:
        if message_format == 'markdown':
            message_body = html2markdown.convert(message)
        if message_format == 'Text':
            h = html2text.HTML2Text()
            h.ignore_links = True
            message_body =  h.handle(message)
        if message_format == 'Html': 
            message_body = message
        return message_body
    except Exception as e:
        print('========Exception===formatmessage====')
        print(str(e))
        return message

def save_image(request):
    if request.method == "POST":
        image = request.FILES.get("image")
        filename = request.POST.get("filename") or image.name
        filepath = os.path.join("static", "app", "usericons", filename)

        if os.path.exists(filepath):
            os.remove(filepath)

        with open(filepath, "wb+") as destination:
            for chunk in image.chunks():
                destination.write(chunk)
        image_url = request.build_absolute_uri(settings.STATIC_URL + "usericons/" + filename)
        return JsonResponse({"message": "Image saved successfully!", "image_url": image_url})
    else:
        return JsonResponse({"message": "Invalid request method."})

def profile_image(request, username):
    # Assuming that the profile images are stored in the media directory
    image_formats = ["jpg", "jpeg", "png", "gif"]
    for ext in image_formats:
        image_path = os.path.join(settings.STATIC_ROOT, "usericons", f"{username}.{ext}")
        if os.path.exists(image_path):
            with open(image_path, "rb") as f:
                image_data = f.read()
            content_type = f"image/{ext}"
            return HttpResponse(image_data, content_type=content_type)
    return HttpResponse(status=404)

def delete_profile_image(request):
    if request.method == 'DELETE':
        data = json.loads(request.body)
        username = data['username']
        extensions = ['jpg', 'jpeg', 'png', 'gif']
        for extension in extensions:
            file_path = os.path.join(settings.STATIC_ROOT, 'usericons/', f'{username}.{extension}')
            if os.path.exists(file_path):
                os.remove(file_path)
                break
        return JsonResponse({'message': 'Profile image deleted successfully'})
    return JsonResponse({'error': 'Invalid request method'})

#mail escalation code:
def escalapolicy(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body.decode('utf-8'))['escalamailData']
            userobj = request.user  # Assuming the user is authenticated
            categories = data.get('categories', '')  # Define this once to use in logs

            policyid = data.get('policyid')
            if policyid:
                policy = policynotifiModel.objects.get(policy_id=policyid)
                policy.subject_category = data.get('subject_category', '')
                policy.categories = data.get('categories', '')
                policy.escalation_mails = json.dumps(data.get('escalation_mails', []))
                policy.definite_mails = json.dumps(data.get('info_mails', []))
                policy.escalation_required = bool(int(data.get('escalation_required', 1)))
                policy.approval_timer = parse_policy_timer(data.get('approval_time'))
                policy.resolution_timer = parse_policy_timer(data.get('resolution_time'))
                policy.save()

                # Log after successful update
                log = AuditlogsModel(username=userobj, action='Update Escalation Mail', status='Success', message=f'Escalation Mail details updated for category: {categories}')
                log.save()

                data['policyid'] = policy.policy_id  # Include this
                return JsonResponse({'status': 200, 'msg': 'Policy updated successfully', 'data': data})
            else:
                policy = policynotifiModel.objects.create(
                    subject_category=data.get('subject_category', ''),
                    categories=data.get('categories', ''),
                    escalation_mails=json.dumps(data.get('escalation_mails', [])),
                    definite_mails=json.dumps(data.get('info_mails', [])),
                    escalation_required=bool(int(data.get('escalation_required', 1))),
                    approval_timer=parse_policy_timer(data.get('approval_time')),
                    resolution_timer=parse_policy_timer(data.get('resolution_time'))
                )

                # Log after successful creation
                log = AuditlogsModel(username=userobj, action='Create Escalation Mail', status='Success', message=f'New Escalation Mail details created for category: {categories}')
                log.save()

                data['policyid'] = policy.policy_id  # Include this
                return JsonResponse({'status': 200, 'msg': 'Policy created successfully', 'data': data})

        except Exception as e:
            # Log after any error occured
            userobj = request.user if request.user.is_authenticated else None
            log = AuditlogsModel(username=userobj, action='Create/Update Escalation Mail', status='Failed', message=f'Error occurred: {str(e)}')
            log.save()
            return JsonResponse({'status': 500, 'msg': f'Error: {str(e)}'})
    return JsonResponse({'status': 405, 'msg': 'Invalid request method'})

def get_escalation_policies(request):
    if request.method == "GET":
        data = []
        policies = policynotifiModel.objects.all()
        for policy in policies:
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
        return JsonResponse({"status": 200, "data": data})
    return JsonResponse({"status": 405, "msg": "Method Not Allowed"})

def edit_escalation_policy(request):
    if request.method == "GET":
        policy_id = request.GET.get("policyid")
        if not policy_id:
            return JsonResponse({"status": 400, "msg": "Missing policy ID"})

        try:
            policy = policynotifiModel.objects.get(policy_id=policy_id)
            data = {
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
            }
            return JsonResponse({"status": 200, "data": data})
        except policynotifiModel.DoesNotExist:
            return JsonResponse({"status": 404, "msg": "Policy not found"})

    return JsonResponse({"status": 405, "msg": "Method Not Allowed"})

def delete_escalation_policy(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            policy_ids = data.get('policy_ids')
            policy_id = data.get('policyid')
            userobj = request.user  # Assuming the user is authenticated

            if policy_ids is not None and isinstance(policy_ids, list) and len(policy_ids) > 0:
                count, _ = policynotifiModel.objects.filter(policy_id__in=policy_ids).delete()
                log = AuditlogsModel(username=userobj, action='Delete Escalation Mail Bulk', status='Success', message=f'Bulk deleted {count} Escalation Mail policies.')
                log.save()
                return JsonResponse({'status': 200, 'msg': f'{count} policies deleted successfully'})
            elif policy_id is not None:
                policy = policynotifiModel.objects.filter(policy_id=policy_id).first()
                if policy:
                    category = policy.categories  # ✅ Get category before delete
                    policy.delete()

                    # Log after successful delete
                    log = AuditlogsModel(username=userobj, action='Delete Escalation Mail', status='Success', message=f'Escalation Mail details deleted for category: {category}')
                    log.save()

                    return JsonResponse({'status': 200, 'msg': 'Policy deleted successfully'})
                else:
                    return JsonResponse({'status': 404, 'msg': 'Policy not found'})
            else:
                return JsonResponse({'status': 400, 'msg': 'Policy ID is required'})

        except Exception as e:
            userobj = request.user if request.user.is_authenticated else None
            log = AuditlogsModel(username=userobj, action='Delete Escalation Mail', status='Failed', message=f'Error occurred while deleting Escalation Mail details: {str(e)}')
            log.save()
            return JsonResponse({'status': 500, 'msg': str(e)})

    return JsonResponse({'status': 405, 'msg': 'Invalid request method'})

@csrf_exempt
def toggle_email_notification(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            event_title = data.get('title') # Get the title passed from frontend
            action = data.get('action') # 'get' or 'update'

            if not event_title:
                return JsonResponse({'status': 400, 'msg': 'Title is required.'})

            # Handle 'get' action to retrieve status
            if action == 'get':
                with connection.cursor() as cursor:
                    cursor.execute("SELECT email_notify FROM notification_system.events WHERE title = %s", [event_title])
                    row = cursor.fetchone()
                    if row:
                        current_status = row[0]
                        # Handling None or other values, default to 1 (Resumed) if not set? 
                        # Assuming 1 is Resume, 0 is Pause.
                        return JsonResponse({'status': 200, 'current_status': current_status})
                    else:
                        # If event not found, maybe default to Resumed (1) or handle as error?
                        # Let's return -1 or None to indicate not found/unknown
                        return JsonResponse({'status': 404, 'msg': 'Event not found'})

            # Update logic (default or action='update')
            status = data.get('status') # 1 for Resume (True), 0 for Pause (False)
            
            # Ensure status is integer
            try:
                status_val = int(status)
                if status_val not in [0, 1]:
                    raise ValueError("Status must be 0 or 1")
            except (ValueError, TypeError):
                 return JsonResponse({'status': 400, 'msg': 'Invalid status value. Must be 0 or 1.'})
            
            # Use raw SQL to update the separate database table
            with connection.cursor() as cursor:
                 # Check if the row exists first using the 'title' column
                 cursor.execute("SELECT count(*) FROM notification_system.events WHERE title = %s", [event_title])
                 row = cursor.fetchone()
                 if row and row[0] > 0:
                     # Update email_notify column
                     cursor.execute("UPDATE notification_system.events SET email_notify = %s WHERE title = %s", [status_val, event_title])
                     msg = "Email notifications resumed" if status_val == 1 else "Email notifications paused"
                 else:
                     return JsonResponse({'status': 404, 'msg': f'Event with title "{event_title}" not found in notification_system.events'})

            # Log the action
            userobj = request.user if request.user.is_authenticated else None
            log = AuditlogsModel(username=userobj, action='Toggle Email Notification', status='Success', message=f'{msg} for {event_title}')
            log.save()
            
            return JsonResponse({'status': 200, 'msg': msg})
            
        except Exception as e:
            return JsonResponse({'status': 500, 'msg': str(e)})
            
    return JsonResponse({'status': 405, 'msg': 'Invalid request method'})

@csrf_exempt
def snooze_email_notification(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            event_title = data.get('title')
            duration = int(data.get('duration', 0))
            unit = data.get('unit', 'minutes')
            site = data.get('site', 'Unknown')
            ip = data.get('ip', 'Unknown')
            nodeid = data.get('nodeid')

            if not event_title or duration <= 0:
                return JsonResponse({'status': 400, 'msg': 'Title and duration are required.'})

            # Calculate seconds for snooze
            seconds = 0
            if unit == 'minutes':
                seconds = duration * 60
            elif unit == 'hours':
                seconds = duration * 3600
            elif unit == 'days':
                seconds = duration * 86400
            else:
                return JsonResponse({'status': 400, 'msg': 'Invalid unit.'})

            # Fetch Device Label from Neo4j
            device_label = "Hardware" # Default
            try:
                # Use Node() directly instead of Notification() to avoid logging errors on Windows
                node = Node()
                # Find the node with the given title and return its label
                query = f"MATCH (n {{title: '{event_title}'}}) RETURN n.label as label"
                result = node.execute(query, ret=True)
                print(f"Neo4j Query for label: {query}")
                print(f"Neo4j Result: {result}")
                
                if result:
                    # Result might be a list of dicts (Bolt) or list of lists (REST)
                    first_row = result[0]
                    if isinstance(first_row, dict):
                        device_label = first_row.get('label', 'Hardware')
                    elif isinstance(first_row, (list, tuple)):
                        device_label = first_row[0] if first_row else 'Hardware'
                
                if not device_label or str(device_label).lower() == 'none':
                    device_label = "Hardware" # Fallback
                
                print(f"Determined device label: {device_label}")
            except Exception as e:
                print(f"Error fetching label: {str(e)}")
                # Fallback to Hardware if anything fails
                device_label = "Hardware"

            # Fetch Policy for the category
            to_emails = []
            cc_emails = []
            try:
                policy = policynotifiModel.objects.filter(categories__iexact=device_label).first()
                if policy:
                    # mmails are stored as JSON strings in the DB based on other views
                    to_emails = json.loads(policy.escalation_mails)
                    raw_cc_emails = json.loads(policy.definite_mails)
                    
                    # Deduplicate: CC should not contain emails already in TO (Case-Insensitive)
                    to_emails_lower = [m.lower() for m in to_emails]
                    cc_emails = [m for m in raw_cc_emails if m.lower() not in to_emails_lower]
            except Exception as e:
                print(f"Error fetching policy: {str(e)}")

            if not to_emails:
                # Fallback or error? If no policy, we might not know who to send to.
                # However, user provided rajkumar.ashokan@finspot.in in example.
                pass

            # Construct Email
            now = datetime.datetime.now()
            # 12-hour format with AM/PM, removing leading zero for hour
            start_time = now.strftime("%I:%M %p").lstrip('0')
            end_time = (now + datetime.timedelta(seconds=seconds)).strftime("%I:%M %p").lstrip('0')
            date_str = now.strftime("%d/%m/%Y")

            # Construct HTML Email Body
            subject = f"Activity Notification - {site}"
            email_html = f"""
            <html>
            <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f4f7f6;">
                <div style="max-width: 600px; margin: 20px auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                    <!-- Header -->
                    <div style="background-color: #1a237e; color: #ffffff; padding: 30px 40px; text-align: center;">
                        <h1 style="margin: 0; font-size: 24px; letter-spacing: 1px;">LinkedInEye Activity Alert</h1>
                    </div>
                    
                    <!-- Content -->
                    <div style="padding: 40px;">
                        <p style="font-size: 16px; color: #333333; line-height: 1.6;">Hi,</p>
                        <p style="font-size: 16px; color: #333333; line-height: 1.6;">
                            Please be informed that activity was observed on the device with the details mentioned below.
                        </p>
                        
                        <!-- Details Table -->
                        <div style="background-color: #f8f9fa; border-left: 4px solid #1a237e; padding: 20px; margin: 25px 0;">
                            <table style="width: 100%; border-collapse: collapse;">
                                <tr>
                                    <td style="padding: 8px 0; color: #666666; font-size: 14px; width: 40%;">Client Name</td>
                                    <td style="padding: 8px 0; color: #1a237e; font-weight: bold; font-size: 15px;">{site}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px 0; color: #666666; font-size: 14px;">Device IP Address</td>
                                    <td style="padding: 8px 0; color: #1a237e; font-weight: bold; font-size: 15px;">{ip}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px 0; color: #666666; font-size: 14px;">Date</td>
                                    <td style="padding: 8px 0; color: #1a237e; font-weight: bold; font-size: 15px;">{date_str}</td>
                                </tr>
                                <tr>
                                    <td style="padding: 8px 0; color: #666666; font-size: 14px;">Time Period</td>
                                    <td style="padding: 8px 0; background-color: #e8eaf6; color: #1a237e; font-weight: bold; font-size: 15px; border-radius: 4px; padding-left: 10px;">
                                        {start_time} to {end_time}
                                    </td>
                                </tr>
                            </table>
                        </div>
                        
                        <p style="font-size: 15px; color: #d32f2f; font-weight: 500; text-align: center; margin-top: 30px;">
                            ⚠️ During this activity time period, please do not check or access this device.
                        </p>
                        
                        <p style="font-size: 14px; color: #666666; margin-top: 30px;">
                            Kindly acknowledge and let us know if any clarification is required.
                        </p>
                    </div>
                    
                    <!-- Footer -->
                    <div style="background-color: #eeeeee; color: #777777; padding: 20px; text-align: center; font-size: 12px;">
                        <p style="margin: 0;">Best regards,</p>
                        <p style="margin: 5px 0 0 0; font-weight: bold; color: #1a237e;">LE Team</p>
                        <p style="margin: 15px 0 0 0;">&copy; 2026 LinkedEye - Managed Security Services</p>
                    </div>
                </div>
            </body>
            </html>
            """

            # Send Email via Office365 SMTP
            if to_emails:
                try:
                    smtp_server = "smtp.office365.com"
                    smtp_port = 587
                    smtp_user = getattr(settings, 'LINKEDEYE_EMAIL', '')
                    smtp_pass = getattr(settings, 'LINKEDEYE_EMAIL_APPKEY', '')

                    msg = MIMEMultipart()
                    msg['From'] = f"Eva <{smtp_user}>"
                    msg['To'] = ", ".join(to_emails)
                    if cc_emails:
                        msg['Cc'] = ", ".join(cc_emails)
                    msg['Subject'] = subject

                    msg.attach(MIMEText(email_html, 'html'))

                    context = ssl.create_default_context()
                    with smtplib.SMTP(smtp_server, smtp_port) as server:
                        server.starttls(context=context)
                        server.login(smtp_user, smtp_pass)
                        # sendmail needs strings, if Cc is used it must be in the to_addrs list
                        recipients = to_emails + cc_emails
                        server.sendmail(smtp_user, recipients, msg.as_string())
                        print(f"Snooze email sent successfully to {to_emails}")
                except Exception as e:
                    print(f"SMTP Error: {str(e)}")

            # Update database to pause notifications (email_notify = 0)
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM notification_system.events WHERE title = %s", [event_title])
                row = cursor.fetchone()
                if row and row[0] > 0:
                    cursor.execute("UPDATE notification_system.events SET email_notify = 0 WHERE title = %s", [event_title])
                    msg = f"Email notifications snoozed for {duration} {unit}"
                else:
                    return JsonResponse({'status': 404, 'msg': f'Event with title "{event_title}" not found'})

            # CRITICAL FIX #5: Use Celery instead of threading.Timer
            # Benefits: No extra threads, uses RabbitMQ worker pool, persistent across restarts
            from notification.tasks import resume_email_notifications
            resume_email_notifications.apply_async(args=[event_title], countdown=seconds)

            # Log the action
            userobj = request.user if request.user.is_authenticated else None
            log = AuditlogsModel(username=userobj, action='Snooze Email Notification', status='Success', message=f'{msg} for {event_title}')
            log.save()
            
            return JsonResponse({'status': 200, 'msg': msg})
            
        except Exception as e:
            return JsonResponse({'status': 500, 'msg': str(e)})
            
    return JsonResponse({'status': 405, 'msg': 'Invalid request method'})


# ---------------------------------------------------------------------------
# Device-wise Alert Policy (Lemonn)
# ---------------------------------------------------------------------------

# Categories available per device type
_DEVICE_CATEGORIES = {
    'Server':   ['Hardware', 'Software', 'Ping'],
    'Switch':   ['Hardware', 'Port', 'Ping'],
    'Firewall': ['Hardware', 'Port', 'Ping'],
    'Router':   ['Hardware', 'Port', 'Ping'],
}

# Map newonb model fields to device_type label
def _get_device_type(obj):
    selecthost = (obj.selecthost or '').lower()
    pathhost = (obj.pathhost or '').lower()
    
    # Check for network devices first
    if 'switch' in pathhost or 'switch' in selecthost:
        return 'Switch'
    if 'firewall' in pathhost or 'firewall' in selecthost or 'fortigate' in pathhost or 'fortigate' in selecthost:
        return 'Firewall'
    if 'router' in pathhost or 'router' in selecthost:
        return 'Router'
        
    # Check for servers
    if 'vm' in pathhost or 'physical' in pathhost or 'server' in selecthost or 'ubuntu' in selecthost or 'centos' in selecthost or 'windows' in selecthost:
        return 'Server'
        
    return None

def get_devices_by_type(request):
    """GET /notification/get_devices_by_type?device_type=Server
    Returns list of devices (ip + friendly name) from newonb for the given type.
    """
    if request.method != 'GET':
        return JsonResponse({'status': 405, 'msg': 'Method Not Allowed'})

    device_type = request.GET.get('device_type', '').strip()
    if not device_type:
        return JsonResponse({'status': 400, 'msg': 'device_type is required'})

    try:
        devices = []
        qs = allonboardModel.objects.all()
        for obj in qs:
            dt = _get_device_type(obj)
            if dt and dt.lower() == device_type.lower():
                devices.append({
                    'ip': obj.ipaddress,
                    'friendly_name': obj.textname or obj.ipaddress,
                    'device_type': dt,
                })

        # deduplicate by IP
        seen = set()
        unique = []
        for d in devices:
            if d['ip'] not in seen:
                seen.add(d['ip'])
                unique.append(d)
        return JsonResponse({'status': 200, 'data': unique})
    except Exception as e:
        return JsonResponse({'status': 500, 'msg': str(e)})


def get_device_alert_categories(request):
    """GET /notification/get_device_alert_categories?device_type=Server
    Returns the alert categories available for a given device type.
    """
    if request.method != 'GET':
        return JsonResponse({'status': 405, 'msg': 'Method Not Allowed'})

    device_type = request.GET.get('device_type', '').strip()
    categories = _DEVICE_CATEGORIES.get(device_type, [])
    return JsonResponse({'status': 200, 'categories': categories})


def save_device_alert_policy(request):
    """POST /notification/save_device_alert_policy
    Create or update a device-level escalation policy stored in the `policy` table.
    Body JSON:
    {
        "id": <optional int for update>,
        "device_type": "Server",
        "device_ip": "10.10.10.5",
        "device_friendly_name": "App-Server",
        "alert_category": "Hardware",
        "escalation_mails": ["a@b.com"],
        "info_mails": ["c@d.com"],
        "escalation_required": 1,
        "approval_time": "30",
        "resolution_time": "60",
        "is_enabled": 1
    }
    """
    if request.method != 'POST':
        return JsonResponse({'status': 405, 'msg': 'Method Not Allowed'})

    try:
        data = json.loads(request.body.decode('utf-8'))
        userobj = request.user

        device_ip = data.get('device_ip', '').strip()
        alert_category = data.get('alert_category', '').strip()
        device_type = data.get('device_type', '').strip()
        device_friendly_name = data.get('device_friendly_name', '').strip()
        escalation_required = bool(int(data.get('escalation_required', 1)))
        is_enabled = bool(int(data.get('is_enabled', 1)))
        approval_timer = parse_policy_timer(data.get('approval_time'))
        resolution_timer = parse_policy_timer(data.get('resolution_time'))
        escalation_mails = json.dumps(data.get('escalation_mails', []))
        info_mails = json.dumps(data.get('info_mails', []))
        subject_category = data.get('subject_category', '').strip()

        record_id = data.get('id')
        if record_id:
            # Update existing row in policy table
            policy = policynotifiModel.objects.get(policy_id=record_id)
            policy.device_type = device_type
            policy.device_ip = device_ip
            policy.device_friendly_name = device_friendly_name
            policy.subject_category = subject_category
            policy.categories = alert_category
            policy.escalation_mails = escalation_mails
            policy.definite_mails = info_mails
            policy.escalation_required = escalation_required
            policy.approval_timer = approval_timer
            policy.resolution_timer = resolution_timer
            policy.is_enabled = is_enabled
            policy.save()
            action = 'Update Device Alert Policy'
            msg = 'Device alert policy updated successfully'
        else:
            # Upsert: match by device_ip + categories (alert_category)
            policy, created = policynotifiModel.objects.update_or_create(
                device_ip=device_ip,
                categories=alert_category,
                defaults={
                    'subject_category': subject_category,
                    'device_type': device_type,
                    'device_friendly_name': device_friendly_name,
                    'escalation_mails': escalation_mails,
                    'definite_mails': info_mails,
                    'escalation_required': escalation_required,
                    'approval_timer': approval_timer,
                    'resolution_timer': resolution_timer,
                    'is_enabled': is_enabled,
                }
            )
            action = 'Create Device Alert Policy'
            msg = 'Device alert policy created successfully' if created else 'Device alert policy updated successfully'

        log = AuditlogsModel(
            username=userobj,
            action=action,
            status='Success',
            message=f'{msg} for IP: {device_ip}, Category: {alert_category}'
        )
        log.save()
        return JsonResponse({'status': 200, 'msg': msg, 'id': policy.policy_id})

    except Exception as e:
        userobj = request.user if request.user.is_authenticated else None
        AuditlogsModel(username=userobj, action='Save Device Alert Policy', status='Failed',
                       message=f'Error: {str(e)}').save()
        return JsonResponse({'status': 500, 'msg': str(e)})


def get_device_alert_policies(request):
    """GET /notification/get_device_alert_policies?device_ip=10.10.10.5
    Returns device-wise policies from the `policy` table (rows where device_ip is set).
    """
    if request.method != 'GET':
        return JsonResponse({'status': 405, 'msg': 'Method Not Allowed'})

    try:
        device_ip = request.GET.get('device_ip', '').strip()
        # Device-wise rows always have device_ip populated
        qs = policynotifiModel.objects.exclude(device_ip='')
        if device_ip:
            qs = qs.filter(device_ip=device_ip)

        data = []
        for p in qs:
            data.append({
                'id': p.policy_id,
                'device_type': p.device_type,
                'device_ip': p.device_ip,
                'device_friendly_name': p.device_friendly_name,
                'subject_category': p.subject_category,
                'alert_category': p.categories,
                'escalation_mails': p.escalation_mails,
                'info_mails': p.definite_mails,
                'escalation_required': 'Enabled' if p.escalation_required else 'Disabled',
                'escalation_required_bool': p.escalation_required,
                'approval_time': p.approval_timer,
                'resolution_time': p.resolution_timer,
                'is_enabled': p.is_enabled,
            })
        return JsonResponse({'status': 200, 'data': data})
    except Exception as e:
        return JsonResponse({'status': 500, 'msg': str(e)})


def edit_device_alert_policy(request):
    """GET /notification/edit_device_alert_policy?id=<policy_id>"""
    if request.method != 'GET':
        return JsonResponse({'status': 405, 'msg': 'Method Not Allowed'})

    policy_id = request.GET.get('id')
    if not policy_id:
        return JsonResponse({'status': 400, 'msg': 'id is required'})

    try:
        p = policynotifiModel.objects.get(policy_id=policy_id)
        return JsonResponse({
            'status': 200,
            'data': {
                'id': p.policy_id,
                'device_type': p.device_type,
                'device_ip': p.device_ip,
                'device_friendly_name': p.device_friendly_name,
                'subject_category': p.subject_category,
                'alert_category': p.categories,
                'escalation_mails': p.escalation_mails,
                'info_mails': p.definite_mails,
                'escalation_required': p.escalation_required,
                'approval_time': p.approval_timer,
                'resolution_time': p.resolution_timer,
                'is_enabled': p.is_enabled,
            }
        })
    except policynotifiModel.DoesNotExist:
        return JsonResponse({'status': 404, 'msg': 'Policy not found'})
    except Exception as e:
        return JsonResponse({'status': 500, 'msg': str(e)})


def delete_device_alert_policy(request):
    """POST /notification/delete_device_alert_policy  Body: {"id": <policy_id>}"""
    if request.method != 'POST':
        return JsonResponse({'status': 405, 'msg': 'Method Not Allowed'})

    try:
        data = json.loads(request.body)
        policy_id = data.get('id')
        userobj = request.user

        if policy_id is None:
            return JsonResponse({'status': 400, 'msg': 'id is required'})

        policy = policynotifiModel.objects.filter(policy_id=policy_id).first()
        if not policy:
            return JsonResponse({'status': 404, 'msg': 'Policy not found'})

        info = f"IP: {policy.device_ip}, Category: {policy.categories}"
        policy.delete()

        AuditlogsModel(username=userobj, action='Delete Device Alert Policy', status='Success',
                       message=f'Deleted device alert policy for {info}').save()
        return JsonResponse({'status': 200, 'msg': 'Policy deleted successfully'})

    except Exception as e:
        userobj = request.user if request.user.is_authenticated else None
        AuditlogsModel(username=userobj, action='Delete Device Alert Policy', status='Failed',
                       message=f'Error: {str(e)}').save()
        return JsonResponse({'status': 500, 'msg': str(e)})

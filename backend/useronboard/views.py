import json
import os
from django.shortcuts import render,HttpResponse
from django.contrib.auth.models import User, auth, Group
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
from django.conf import settings
import datetime
from django.template import loader
from .models import Userapplication, Usersite, PermissionsModel
from bulk_sync import bulk_sync
from django.db.models import Q
from lib.LinkedEyeNotification import Notification
from notification.models import ServiceModel, UserNotificationSetingsModel
from auditlogs.models import AuditlogsModel
from userprofile.models import subsiteModel
from lesites.models import SiteModel
from lib.LinkedEyeKeycloakAdmin import sync as keycloak_sync


def _payload_site_ids(parsed_json):
    return [s.get('id') for s in parsed_json.get('sites', []) if s.get('id')]

@login_required(login_url="/")
@role_required(allowed_roles = ["Admin"])
def useronboard(request):
    template = loader.get_template('useronboard.html')
    context = {
        'roles': Group.objects.all(),
        'data': User.objects.all()
    }
    return HttpResponse(template.render(context, request))  

def _sync_user_applications(user_id, parsed_json):
    Userapplication.objects.filter(user_id=user_id).delete()
    if parsed_json.get('isStoreApplication') is True:
        for app_data in parsed_json.get('applications', []):
            Userapplication(
                user_id=user_id,
                application_id=app_data['id'],
                weightage=app_data['weightage']
            ).save()

def _sync_user_sites(user_id, parsed_json, preserve_payload_status=False):
    Usersite.objects.filter(user_id=user_id).delete()
    for site_data in parsed_json.get('sites', []):
        Usersite(
            user_id=user_id,
            site_id=site_data['id'],
            is_enable=site_data.get('isEnabled', True) if preserve_payload_status else True
        ).save()

def _sync_user_subsites(user_id, parsed_json):
    subsiteModel.objects.filter(user_id=user_id).delete()
    subsite_data = parsed_json.get('subSiteData') or {}
    for site_name, texts in subsite_data.items():
        try:
            site_obj = SiteModel.objects.get(sitename=site_name)
        except SiteModel.DoesNotExist:
            print(f'Site {site_name} not found')
            continue
        for text in texts:
            subsiteModel(user_id=user_id, site_id=site_obj.id, sub_site=text).save()

@login_required(login_url="/")
def useroperations(request):
    response = { }
    if request.method == "POST":
        parsed_json = {}
        try:
            clientData = json.loads(request.POST['alldata'])
            parsed_json = clientData['data']
            operation = parsed_json["operation"]

            # Add/edit/delete/changestatus are account-management actions
            # (create, role change, removal, enable/disable) and must stay
            # DjangoAdmin-only, separate from the page-level "Admin" gate on
            # useronboard() -- an Admin who isn't also DjangoAdmin can view
            # this page but must not be able to add, alter, remove, or
            # disable other users. Membership check, not the weightage-based
            # top_group used elsewhere, since Admin (21) outranks DjangoAdmin
            # (20) and would otherwise win for a dual-group user.
            if operation in ('register', 'update', 'delete', 'changestatus') and not request.user.groups.filter(name='DjangoAdmin').exists():
                response['status'] = 403
                response['msg'] = 'Only DjangoAdmin can add, edit, or delete users'
                response['msg1'] = ' '
                response['errorMsg'] = 'Only DjangoAdmin can add, edit, or delete users'
                return HttpResponse(json.dumps(response), content_type="application/json")

            if operation == 'register':
                firstname = parsed_json['firstname']
                lastname = '-'
                email = parsed_json['email']
                password = parsed_json['password']
                if User.objects.filter(email=email).exists() or User.objects.filter(username=email).exists():
                    response['msg'] = 'Email already exist.'
                    response['status'] = 500
                else:
                    group = Group.objects.get(name=parsed_json['role']).id
                    user = User.objects.create_user(
                        username=email,
                        password=password,
                        email=email,
                        first_name=firstname,
                        last_name=lastname,
                        is_active=True
                    )
                    user.groups.add(group)
                    _sync_user_applications(user.id, parsed_json)
                    _sync_user_sites(user.id, parsed_json, preserve_payload_status=True)
                    _sync_user_subsites(user.id, parsed_json)
                    # Mirror into Keycloak with the SAME password (best-effort;
                    # a Keycloak outage must not fail app onboarding).
                    ok, detail = keycloak_sync.sync_create_user(
                        email=email, first_name=firstname, last_name=lastname,
                        password=password, role=parsed_json['role'],
                        site_ids=_payload_site_ids(parsed_json),
                        subsites=parsed_json.get('subSiteData') or {},
                    )
                    if not ok:
                        print(f"[LE] Keycloak sync (create) skipped/failed for {email}: {detail}")
                    response['status'] = 200
                    response['msg'] = 'User added sucessfully'
                    response['rowid'] = user.id

                    result = send_Welcome_Message(parsed_json)
                    print('--result--updareResponse---')
                    print(result)
                    if result and result.get('data'):
                        response['msg1'] = 'Notification send to user email'
                    else:
                        response['msg1'] = 'For user Not able to send notification.'
                    AuditlogsModel(username=request.user, action='User onboarding', status='Success', message='User '+email+' added sucessfully.').save()

            elif operation == 'update':
                userobj = User.objects.get(id=parsed_json["rowid"])
                userobj.first_name = parsed_json['firstname']
                userobj.save()
                userobj.groups.clear()
                newgroupid = Group.objects.get(name=parsed_json['role']).id
                userobj.groups.add(newgroupid)
                _sync_user_applications(userobj.id, parsed_json)
                _sync_user_sites(userobj.id, parsed_json)
                _sync_user_subsites(userobj.id, parsed_json)
                ok, detail = keycloak_sync.sync_update_user(
                    email=userobj.email or userobj.username,
                    first_name=parsed_json['firstname'], role=parsed_json['role'],
                    site_ids=_payload_site_ids(parsed_json),
                    subsites=parsed_json.get('subSiteData') or {},
                )
                if not ok:
                    print(f"[LE] Keycloak sync (update) skipped/failed for {userobj.email}: {detail}")
                response['status'] = 200
                response['msg'] = 'User updated sucessfully'
                response['rowid'] = parsed_json["rowid"]
                AuditlogsModel(username=request.user, action='Update User', status='Success', message='User '+userobj.email+' updated sucessfully').save()
                return HttpResponse(json.dumps(response), content_type="application/json")

            elif operation == 'delete':
                deleteobj = User.objects.get(id=parsed_json["rowid"])
                _deleted_email = deleteobj.email or deleteobj.username
                AuditlogsModel(username=request.user, action='Delete User', status='Success', message='User '+deleteobj.email+' deleted sucessfully').save()
                deleteobj.delete()
                ok, detail = keycloak_sync.sync_delete_user(_deleted_email)
                if not ok:
                    print(f"[LE] Keycloak sync (delete) skipped/failed for {_deleted_email}: {detail}")
                response['status'] = 200
                response['msg'] = 'User deleted successfully'
                response['rowid'] = parsed_json["rowid"]
                return HttpResponse(json.dumps(response), content_type="application/json")

            elif operation == 'changestatus':
                obj = User.objects.get(id=parsed_json["rowid"])
                if int(parsed_json['rowid']) == 1:
                    print('===Admin cannot be Disabled===')
                    response['msg'] = 'Enable'
                    response['status'] = 200
                    response['errorMsg'] = 'Admin cannot be disabled'
                    AuditlogsModel(username=request.user, action='Change User Staus', status='Success', message='User '+obj.username+' enable successfully').save()
                    return HttpResponse(json.dumps(response), content_type="application/json")

                if parsed_json["status"] == 'Enable':
                    obj.is_active = False
                    obj.save()
                    response['msg'] = 'Disable'
                    response['status'] = 200
                    log_message = 'User '+obj.username+' disable successfully'
                else:
                    obj.is_active = True
                    obj.save()
                    response['msg'] = 'Enable'
                    response['status'] = 200
                    log_message = 'User '+obj.username+' enable successfully'
                AuditlogsModel(username=request.user, action='Change User Staus', status='Success', message=log_message).save()
                ok, detail = keycloak_sync.sync_set_enabled(obj.username, obj.is_active)
                if not ok:
                    print(f"[LE] Keycloak sync (status) skipped/failed for {obj.username}: {detail}")

        except Exception as e:
            print('===Exception===useroperations===')
            print(str(e))
            action = parsed_json.get("operation", "Unknown") if isinstance(parsed_json, dict) else "Unknown"
            AuditlogsModel(username=request.user, action=action+' User', status='Failure', message=str(e)).save()
            response['status'] = 400
            response['msg'] = 'Something went wrong'
            response['msg1'] = str(e)
            response['errorMsg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="application/json")
def getallPermissions(request):
    response = {}
    try:
        permisions_obj = []
        app_obj = PermissionsModel.objects.all()
        if not app_obj:
            permisions_obj = [{'name':'View All','codename':'VA'},{'name':'View Selected Application', 'codename':'VSA'},{'name':'Edit All','codename':'EA'},{'name':'Edit Selected Application','codename':'ESA'}]
            permissions_models = []
            for jsonObj in permisions_obj:
                modelObj = PermissionsModel()
                modelObj.name = jsonObj["name"]
                modelObj.codename = jsonObj["codename"]
                permissions_models.append(modelObj)
            filters = Q()
            key_fields = ("name",)
            result = bulk_sync(new_models=permissions_models, filters=filters, key_fields=key_fields)
            if result:
                response['status'] = 200
                response['data'] = permisions_obj
            else:
                response['status'] = 500
                response['data'] = []
            return HttpResponse(json.dumps(response))
        else:
            permisions_obj = []
            for temp in app_obj:
                json_obj = {}
                json_obj["name"] = temp.name
                json_obj["codename"] = temp.codename
                permisions_obj.append(json_obj)
            response['status'] = 200
            response['data'] = permisions_obj
            return HttpResponse(json.dumps(response))
    except Exception as e:
        print('========Exception======')
        print(str(e))
        response['status'] = 400
        response['error_msg'] = 'Not able to get Permissions'
        return HttpResponse(json.dumps(response))
def addRoles(request):
    response = { }
    try:
        if request.method == "POST":
            clientData = json.loads(request.POST['alldata'])
            parsed_json = clientData['data']
            if Group.objects.filter(name=parsed_json['rolename']).exists():
                response['status'] = 400
                response['error_msg'] = 'Role name already exist'
            else:
                Group(name=parsed_json['rolename'], weightage=parsed_json['weightage']).save()
                response['status'] = 200
                data = {}
                data['rolename'] = parsed_json['rolename']
                data['weightage'] = parsed_json['weightage'] 
                response['data'] = data
            return HttpResponse(json.dumps(response))
    except Exception as e:
        print('========Exception======')
        print(str(e))
        response['status'] = 400
        response['error_msg'] = 'Not able to add Role'
        return HttpResponse(json.dumps(response))
def get_all_groups(request):
    response = {}
    try:
        temp_list = []
        app_obj = Group.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["name"] = temp.name
            json_obj["weightage"] = temp.weightage
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        print('========Exception======')
        print(str(e))
        response['status'] = 400
        response['error_msg'] = 'Not able to get Roles'
        return HttpResponse(json.dumps(response), content_type="application/json")
def get_userlist(request):
    response = {}
    try:
        if request.method == 'POST':
            site_id = json.loads(request.POST['siteId'])
            userid_list = Usersite.objects.filter(site_id=site_id).values_list('user_id', flat=True)
            response['data'] = [x for x in userid_list]
        else:
            temp_list = []
            user_list_obj = User.objects.all()
            for user in user_list_obj:
                json_obj = {}
                json_obj["id"] = user.id
                json_obj["firstname"] = user.first_name
                json_obj["last_name"] = user.last_name
                json_obj["email"] = user.email
                json_obj["is_active"] = user.is_active
                # Was: str(user.groups.all()[0]) if len(user.groups.all())==1
                # else "ViewOnly" -- any user with more than one group (e.g.
                # Admin + DjangoAdmin, a normal combination for the SSO
                # OTP-bypass role) always fell into the "ViewOnly" branch
                # outright, regardless of their actual highest role. This is
                # what the sidebar admin icon's JS (common.js, checks
                # role === "Admin") reads, so no multi-group user could ever
                # see it. Pick the highest-weightage group instead.
                top_group = user.groups.order_by('-weightage').first()
                json_obj["role"] = top_group.name if top_group else "ViewOnly"
                json_obj["date_joined"] = user.date_joined
                temp_list.append(json_obj)
            response['data'] = temp_list
        response['status'] = 200
        return HttpResponse(json.dumps(response, default=convert_timestamp))
    except Exception as e:
        print('========Exception==get_userlist====')
        print(str(e))
        response['status'] = 400
        response['error_msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))
def convert_timestamp(item_date_object):
    if isinstance(item_date_object, (datetime.date, datetime.datetime)):
        return item_date_object.timestamp()
def send_Welcome_Message(user_obj):
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        smtp_host = 'smtp.office365.com'
        smtp_port = 587
        sender = os.getenv('LINKEDEYE_EMAIL', '')
        password = os.getenv('LINKEDEYE_EMAIL_APPKEY', '')
        recipient = user_obj['email']

        if not sender or not password:
            return {'status': 400, 'data': False, 'error_msg': 'LINKEDEYE_EMAIL or LINKEDEYE_EMAIL_APPKEY not set'}

        portal_url = settings.PORTAL_URL or os.getenv('PORTAL_URL') or 'http://127.0.0.1:8000'
        variables = {
            'name': user_obj['firstname'],
            'url': portal_url,
            'username': user_obj['email'],
            'password': user_obj['password'],
        }

        import os as _os
        template_path = _os.path.join(_os.path.dirname(__file__), '..', 'lib', 'LinkedEyeNotification', 'templates', 'onboarding.html')
        try:
            with open(template_path) as f:
                body = f.read()
            for key, val in variables.items():
                body = body.replace('{{' + key + '}}', str(val))
        except Exception:
            body = '<p>Welcome to LinkedEye, {}!</p><p>Username: {}<br>Password: {}<br>Portal: {}</p>'.format(
                variables['name'], variables['username'], variables['password'], variables['url'])

        msg = MIMEMultipart('alternative')
        msg['From'] = sender
        msg['To'] = recipient
        msg['Subject'] = 'Welcome to Linkedeye'
        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(sender, password)
            s.sendmail(sender, recipient, msg.as_string())

        print(f'Welcome mail sent to {recipient}')
        return {'status': 200, 'data': True, 'error_msg': ''}
    except Exception as e:
        print('========Exception===Welcome_Message===')
        print(str(e))
        return {'status': 400, 'data': False, 'error_msg': str(e)}
def change_password(request):
    response = {}
    try:
        clientData = json.loads(request.POST['clientData'])
        parsed_json = clientData['data']
        user = auth.authenticate(username=parsed_json["username"], password=parsed_json["currentpsw"])
        if user is not None:
            userobj =  User.objects.get(username=parsed_json["username"])
            userobj.set_password(parsed_json['newpsw'])
            userobj.save()
            response['status'] = 200
            response['msg'] = 'Password changed successfully'
            log = AuditlogsModel(username = request.user,  action = 'Change Password', status = 'Success', message='For user '+parsed_json["username"]+'password changed successfully')
        else:
            response['status'] = 500
            response['msg'] = 'Username or Password is wrong'
            log = AuditlogsModel(username = request.user,  action = 'Change Password', status = 'Failure', message='Not able to change password for user '+parsed_json["username"])
    except Exception as e:
        print('========Exception===change_password===')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        response['msg1'] = str(e)
        log = AuditlogsModel(username = request.user,  action = 'Change Password', status = 'Failure', message=str(e))
    log.save()
    return HttpResponse(json.dumps(response), content_type="application/json")

def getcurrentuser(request):
    """
    Returns the currently logged-in user's information
    """
    response = {}
    try:
        # Get the current logged-in user
        current_user = request.user
        
        # Check if user is authenticated
        if not current_user.is_authenticated:
            response['status'] = 401
            response['error_msg'] = 'User not authenticated'
            return HttpResponse(json.dumps(response))
        
        # Build user data
        user_data = {
            "id": current_user.id,
            "firstname": current_user.first_name,
            "last_name": current_user.last_name,
            "email": current_user.email,
            "username": current_user.username,
            "is_active": current_user.is_active,
            "role": str(current_user.groups.all()[0]) if current_user.groups.exists() else "ViewOnly",
            "is_django_admin": current_user.groups.filter(name='DjangoAdmin').exists(),
            "date_joined": current_user.date_joined
        }
        
        response['data'] = user_data
        response['status'] = 200
        return HttpResponse(json.dumps(response, default=convert_timestamp))
        
    except Exception as e:
        print('========Exception==get_current_user====')
        print(str(e))
        response['status'] = 500
        response['error_msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def getsubsitedata(request):
    response = {}
    if request.method == "POST":
        try:
            mode = request.POST.get("mode")
            filters = {}
            
            # Filter by user only
            if mode == "user":
                userId = json.loads(request.POST.get("userId"))
                filters["user_id"] = userId
            
            # Filter by site only
            elif mode == "site":
                siteId = json.loads(request.POST.get("siteId"))
                filters["site_id"] = siteId
            
            # ⚡ Filter by BOTH user AND site
            elif mode == "user_site":
                userId = json.loads(request.POST.get("userId"))
                siteId = json.loads(request.POST.get("siteId"))
                filters["user_id"] = userId
                filters["site_id"] = siteId
            
            else:
                response["status"] = 400
                response["data"] = "Invalid mode"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            # Query with filters
            subsites = subsiteModel.objects.filter(**filters).select_related('site')
            
            # Group by site name
            subsite_data = {}
            for subsite in subsites:
                site_name = subsite.site.sitename
                subsite_data.setdefault(site_name, []).append(subsite.sub_site)
            
            response["status"] = 200
            response["data"] = subsite_data
            
        except Exception as e:
            print("===Exception===get_subsite_data===")
            print(str(e))
            response["status"] = 500
            response["data"] = {}
            
    return HttpResponse(json.dumps(response), content_type="application/json")

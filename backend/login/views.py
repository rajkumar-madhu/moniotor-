from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.urls import reverse
from django.contrib import messages
from django.contrib.auth.models import User, auth
from django.db import models
from django.contrib.auth.models import Group
from django.conf import settings
import json
from notification.models import ServiceModel, UserNotificationSetingsModel
from random import randint
from notification.models import ServiceModel
from lib.LinkedEyeNotification import Notification
from django.template.loader import render_to_string
from useronboard.models import Userotp
from auditlogs.models import AuditlogsModel
import os
import logging

logger = logging.getLogger('linkedeye')

ADMIN_DEFAULT_PASSWORD = getattr(settings, 'ADMIN_DEFAULT_PASSWORD', '') or os.getenv('ADMIN_DEFAULT_PASSWORD', '')

def index(request):
    if request.user.is_authenticated:
        if request.user.groups.all()[0].name == 'Admin':
            return redirect('admindashboard:Dashboard')
        else:
            return redirect('dashboard:Dashboard')
    else:
        return render(request, 'login.html')

def verify(request):
    
    nextUrl = request.GET.get('next')
    if request.method == 'POST':
        response = { }
        parsed_json = json.loads(request.POST['alldata'])
        email=parsed_json['username']
        password=parsed_json['password']
        if not User.objects.filter(username=email).exists():
            if email != "admin":
                response['msg'] = "The username does not exist. Try again"
                response["status"] = 500
            else:
                if Group.objects.filter(name='Admin').exists():
                    pass
                else:
                    ob_role = Group(name='Admin', weightage = 21) # 21=010101 ['VSA','VA','ESA','EA','DSA','DA']
                    ob_role.save()
                group = Group.objects.get(name = 'Admin').id
                user = User.objects.create_user(username=email,password=ADMIN_DEFAULT_PASSWORD,email=email,first_name=email,last_name=email,is_active=True)
                user.save()
                user.groups.add(group)
                auth.login(request, user)
                response["status"] = 200
                response["redirectUrl"] = '/admindashboard'
        else:
            obj = User.objects.get(username=email)
            if obj.is_active == True:
                user = auth.authenticate(username=email,password=password)
                if user is None and getattr(settings, 'KEYCLOAK_ENABLED', False):
                    # Local password check failed (wrong password, or the account
                    # has no usable password — e.g. it was auto-created by an SSO
                    # login and never onboarded through the app). Fall back to
                    # Keycloak; on success, cache the password locally so future
                    # logins work from MySQL too.
                    from login.keycloak_utils import verify_password_via_keycloak
                    if verify_password_via_keycloak(obj.email or email, password):
                        obj.set_password(password)
                        obj.save(update_fields=['password'])
                        user = auth.authenticate(username=email, password=password)
                if user is not None:
                    auth.login(request, user)
                    if nextUrl is None:
                        response["status"] = 200
                        services = ServiceModel.objects.filter()
                        if services:
                            service_ids = []
                            for service in services:
                                service_ids.append(service.id)
                                if UserNotificationSetingsModel.objects.filter(service_id = service.id,user_id=obj.id, is_saved = True).exists():
                                    if request.user.groups.all()[0].name == 'Admin':
                                        response["redirectUrl"] = '/admindashboard'
                                    else:
                                        response["redirectUrl"] = '/dashboard'
                                else:
                                    if request.user.groups.all()[0].name == 'Admin':
                                        response["redirectUrl"] = '/profile?next=/admindashboard'
                                    else:
                                        response["redirectUrl"] = '/profile?next=/dashboard' 
                        else:
                            if request.user.groups.all()[0].name == 'Admin':
                                response["redirectUrl"] = '/admindashboard'
                            else:
                                response["redirectUrl"] = '/dashboard'
                    else:
                        response["status"] = 200
                        response["redirectUrl"] = nextUrl          
                else:
                    response["status"] = 500
                    response["msg"] = "The username or password you entered is incorrect. Try again"
            else:
                response["status"] = 500
                response["msg"] = "The account has been disabled. Contact the Admin!"
    if request.user.is_authenticated:
        request.session['user_permissions'] = get_user_permissions(request.user.groups.all()[0].name)
    if response["status"] == 200:
        log = AuditlogsModel(username = request.user,  action = 'User login', status = 'Success', message='User '+email+' login  successfully.')
    else:
        log = AuditlogsModel(username = request.user,  action = 'User login', status = 'Failure', message='User '+email+' not able to login')
    log.save()
    return HttpResponse(json.dumps(response), content_type="json")
def logout(request):
    response = { }
    # Capture before auth.logout()/session.flush() wipe the session - this is
    # only present for users who came in via Finspot SSO (mozilla-django-oidc
    # stores it when OIDC_STORE_ID_TOKEN=True).
    oidc_id_token = request.session.get('oidc_id_token')
    if request.session.has_key('user_permissions'):
        request.session.flush()
    try:
        auth.logout(request)
        response["status"] = 200
        # Logging out locally doesn't end the Keycloak SSO session, so
        # revisiting the app silently re-authenticates via Keycloak (no
        # login prompt) and lands straight back on the OTP gate. Redirect
        # through Keycloak's own end-session endpoint too when this was an
        # SSO session, so the next visit needs a real Keycloak login again.
        logout_endpoint = getattr(settings, 'OIDC_OP_LOGOUT_ENDPOINT', '')
        if oidc_id_token and logout_endpoint:
            post_logout_uri = getattr(settings, 'OIDC_RP_POST_LOGOUT_REDIRECT_URI', '/')
            from urllib.parse import urlencode
            response["redirectUrl"] = logout_endpoint + '?' + urlencode({
                'id_token_hint': oidc_id_token,
                'post_logout_redirect_uri': post_logout_uri,
            })
        else:
            response["redirectUrl"] = '/'
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Not able to logout'
    return HttpResponse(json.dumps(response), content_type="json")

def get_user_permissions(userGroup):
    userPermissions = []
    weightage = Group.objects.get(name = userGroup).weightage
    binaryValue = decimalToBinary(weightage) 
    permisionOrder = ['VSA','VA','ESA','EA','DSA','DA']
    for bValue, permision in zip(reversed(binaryValue), reversed(permisionOrder)):
        if bValue == '1':
            userPermissions.append(permision)
    
    return userPermissions
def decimalToBinary(number):
    result = ''
    while number != 0:
        remainder = number % 2  # gives the exact remainder
        number = number // 2
        result = str(remainder) + result
    return result
def random_with_N_digits(n):
    range_start = 10**(n-1)
    range_end = (10**n)-1
    return randint(range_start, range_end)

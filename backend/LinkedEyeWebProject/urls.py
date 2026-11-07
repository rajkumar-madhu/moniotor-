"""
Definition of urls for LinkedEyeWebProject.
"""

from datetime import datetime
from django.urls import path, include,re_path
from django.contrib import admin
from django.contrib.auth.views import LoginView
from app import forms, views
#from django.confs.urls.defaults import *
from django.conf import settings
from django.views.static import serve
from django.http import HttpResponse

from ms_identity_web.django.msal_views_and_urls import MsalViews
from LinkedEyeWebProject import metrics

msal_urls = MsalViews(settings.MS_IDENTITY_WEB).url_patterns()
#print('common-url---------------->')
urlpatterns = [
    # CRITICAL FIX #3 & #25: Health and metrics endpoints
    path('health/', metrics.health_view, name='health'),
    path('metrics/', metrics.metrics_view, name='metrics'),
    
    path('', views.home, name='home'),
    # MDI ships sourceMappingURL comments pointing at .css.map files that aren't
    # collected; the server returns them as application/octet-stream and the
    # browser refuses the stylesheet. Serve an empty text/css so the map request
    # succeeds quietly instead of erroring.
    path(
        'static/app/vendors/mdi/css/materialdesignicons.min.css.map',
        lambda request: HttpResponse('', content_type='text/css'),
        name='mdi_min_css_map_noop',
    ),
    path(
        'static/app/vendors/mdi/css/materialdesignicons.css.map',
        lambda request: HttpResponse('', content_type='text/css'),
        name='mdi_css_map_noop',
    ),
    # Runtime STOMP credentials for the dashboards. Deliberately a view, not a
    # static file: it must track linkedeye-mq-secret and must not be readable
    # anonymously the way /static/**.js is.
    path('ws-config.js', views.ws_config, name='ws_config'),
    path('dashboard/', views.dashboard, name='overview'),
    path('useronboard/', include('useronboard.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('comparision/', views.comparision, name='comparision'),
    path('comparision/', include('comparision.urls')),
    path('bod-eodstatus/', include('bodeodstatus.urls')),
    path('lesites/', include('lesites.urls')),
    path('onboard/', include('onboarding.urls')),
    path('allonboard/', include('allonboard.urls')),
    path('autodiscover/', include('autodiscover.urls')),
    path('notification/', include('notification.urls')),
    path('notificationsettings/', include('notification.urls')),
    path('entity/', include('entity.urls')),
    path('auditlogs/', include('auditlogs.urls')),
    path('vault/', include('vault.urls')),
    path('addservice/', include('addservice.urls')),
    path('applications/', include('applications.urls')),
    
    path('siteError/', views.siteErr, name='siteError'),
    path('invalidDomain/', views.invalidDomain, name='invalidDomain'),
    path('india/', views.india, name='india'),
    path('switch/', views.switch, name='switch'),
    path('calendar/', views.calendar, name='calendar'),
    path('calendar/getCalendarData', views.get_calendar_data, name='getCalendarData'),
    path('prometheus/proxy/', views.prometheus_proxy, name='prometheus_proxy'),
    path('grafana/token/', views.grafana_generate_token, name='grafana_generate_token'),
    # Full Grafana reverse proxy — catch-all for dashboard HTML + all sub-resources (JS/CSS/API)
    re_path(r'^grafana-proxy/(?P<path>.*)$', views.grafana_full_proxy, name='grafana_full_proxy'),
    # Catch stray Grafana requests that don't respect the proxy path (SystemJS, avatars, APIs)
    re_path(r'^(?P<path>(public|api|apis|avatar)/.*)$', views.grafana_full_proxy, name='grafana_stray_proxy'),
    path('profile/', include('userprofile.urls')),
    path('lesites/', views.sites, name='lesites'),
    path('lesites/', include('lesites.urls')),
    path('sitehealth/', include('sitehealth.urls')),
    path('hsonboard/', include('hsonboarding.urls')),
    # A plain LogoutView only ends the Django session; when the user came in
    # via Finspot SSO, Keycloak's own session survives it and silently
    # re-authenticates them on the next visit. keycloak_aware_logout ends the
    # Keycloak session too when applicable, falling back to next_page='/'
    # behavior otherwise.
    path('logout/', views.keycloak_aware_logout, name='logout'),
    path('leadmin/', views.leadmin, name='leadmin'),
    path('admin/', admin.site.urls),
    path('getfilecontent/', views.getfilecontent, name='getfilecontent'),
    path('getfilecontents/', views.getfilecontents, name='getfilecontents'),
    path('getsnmpfilecontent/', views.getsnmpfilecontent, name='getsnmpfilecontent'),
    path('login/',
         LoginView.as_view
         (
             template_name='app/login.html',
             authentication_form=forms.BootstrapAuthenticationForm,
             extra_context=
             {
                 'title': 'Log in',
                 'year' : datetime.now().year,
             }
         ),
         name='login'),
    path('login/verify', views.verify, name='verify'),
    path('verify-otps/', views.verify_otps, name='verify_otps'),
    path('resend-otps/', views.resend_otps, name='resend_otps'),
    path('setup-google-authenticator/', views.setup_google_authenticator, name='setup_google_authenticator'),
    path('verify-google-authenticator-setup/', views.verify_google_authenticator_setup, name='verify_google_authenticator_setup'),
    path('disable-google-authenticator/', views.disable_google_authenticator, name='disable_google_authenticator'),
    path('check-google-authenticator-status/', views.check_google_authenticator_status, name='check_google_authenticator_status'),
    path('verify-google-authenticator-login/', views.verify_google_authenticator_login, name='verify_google_authenticator_login'),
    path('login/google_verify', views.google_verify, name='google_verify'),
    path('login/keycloak_verify', views.keycloak_verify, name='keycloak_verify'),
    path('analytics/', include('analytics.urls')),
    path('incidents/', include('incidents.urls')),
    path('login/generateOtp', views.generate_otp, name='generateotp'),
    path('login/verifyOTP', views.verify_OTP, name='verifyOTP'),
    path('login/forgotPassword', views.forgot_password, name='forgotPassword'),
    path('login/getuserinfo', views.getuserinfo, name='getuserinfo'),
    path("accounts/", include("allauth.urls")),
    re_path(r'^media/(?P<path>.*)$', serve,{'document_root': settings.MEDIA_ROOT}),
    re_path(r'^static/(?P<path>.*)$', serve,{'document_root': settings.STATIC_ROOT}),
    
    path('auth/redirect', views.ms_verify, name='ms_verify'),
    path(f'{settings.AAD_CONFIG.django.auth_endpoints.prefix}/', include(msal_urls)),
    
    # HIGH FIX #3: Health check endpoint for K8s probes
    path('health/', views.health_check, name='health_check'),
    path('health/check/', views.health_check, name='health'),
]

if getattr(settings, 'KEYCLOAK_ENABLED', False):
    from login.keycloak_views import (
        FinspotStaffOIDCAuthenticationRequestView,
        KeycloakCallbackView,
    )

    urlpatterns += [
        path(
            'auth/oidc/authenticate/',
            FinspotStaffOIDCAuthenticationRequestView.as_view(),
            name='oidc_authentication_init',
        ),
        path(
            'auth/oidc/callback/',
            KeycloakCallbackView.as_view(),
            name='oidc_authentication_callback',
        ),
    ]

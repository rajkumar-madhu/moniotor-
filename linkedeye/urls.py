from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import views as core
from core.auth_views import OTPLoginView, resend_otp, verify_otp
from domainmonitor.urls import api_urlpatterns as domain_api

admin.site.site_header = "LinkedEye administration"
admin.site.site_title = "LinkedEye"   # admin.site.login_form is set in AdministrationConfig.ready()

urlpatterns = [
    path("welcome/", core.b2b_landing, name="b2b_landing"),
    path("", core.dashboard, name="dashboard"),
    path("search/", core.search, name="search"),
    path("healthz", core.healthz, name="healthz"),
    path("api/backend-summary/", core.backend_summary_api, name="backend_summary_api"),
    path("accounts/login/", OTPLoginView.as_view(), name="login"),
    path("accounts/verify-otp/", verify_otp, name="verify_otp"),
    path("accounts/resend-otp/", resend_otp, name="resend_otp"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("monitoring/url-checker/", include("urlchecker.urls")),
    path("legacy/heatmap/", core.heatmap, name="heatmap"),
    path("legacy/exporters/", core.exporters, name="exporters"),
    path("legacy/incidents/", core.incidents, name="incidents"),
    path("legacy/fg-01/", core.firewall, name="firewall"),
    path("domains/", include("domainmonitor.urls")),
    path("configuration/domains/", include(domain_api)),
    # module apps define full paths in their own urls.py
    path("", include("servicedesk.urls")),
    path("", include("cmdb.urls")),
    path("", include("network.urls")),
    path("", include("telemetry.urls")),
    path("", include("observe.urls")),
    path("", include("tcpcheck.urls")),
    path("", include("bodeod.urls")),
    path("", include("automation.urls")),
    path("", include("administration.urls")),
    path("", include("reports.urls")),
    path("", include("agent.urls")),
    path("", include("ipam.urls")),
    path("", include("dcim.urls")),
    path("", include("compute.urls")),
    path("", include("onboarding.urls")),
    path("", include("k8s.urls")),
    path("", include("apm.urls")),
    path("", include("hardware.urls")),
    path("", include("hypervisor.urls")),
    path("", include("architecture.urls")),
    path("soon/<slug:slug>/", core.coming_soon, name="coming_soon"),
    path("admin/", admin.site.urls),
]

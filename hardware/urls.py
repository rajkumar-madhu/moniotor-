from django.urls import path

from . import views

app_name = "hardware"
urlpatterns = [
    # one server's data (device = network.Device pk)
    path("hardware/server/<int:device_pk>/live/", views.live, name="live"),
    path("hardware/server/<int:device_pk>/refresh/", views.refresh, name="refresh"),
    path("hardware/server/<int:device_pk>/vendor/", views.vendor, name="vendor"),
    path("hardware/server/<int:device_pk>/inventory/", views.inventory, name="inventory"),
    path("hardware/server/<int:device_pk>/events/", views.events, name="events"),
    path("hardware/server/<int:device_pk>/report.csv", views.export, name="export"),
    path("hardware/server/<int:device_pk>/alerts/", views.alerts, name="alerts"),
    path("hardware/server/<int:device_pk>/requests/new/", views.request_new, name="request_new"),
    # control requests (records only)
    path("hardware/requests/", views.requests_list, name="requests"),
    path("hardware/requests/<int:pk>/decide/", views.request_decide, name="request_decide"),
    # endpoints admin
    path("hardware/endpoints/", views.endpoints, name="endpoints"),
    path("hardware/endpoints/new/", views.endpoint_new, name="endpoint_new"),
    path("hardware/endpoints/<int:pk>/", views.endpoint_edit, name="endpoint_edit"),
    path("hardware/endpoints/<int:pk>/test/", views.endpoint_test, name="endpoint_test"),
    path("hardware/endpoints/<int:pk>/pin/", views.endpoint_pin, name="endpoint_pin"),
    path("hardware/endpoints/<int:pk>/toggle/", views.endpoint_toggle, name="endpoint_toggle"),
    path("hardware/endpoints/<int:pk>/delete/", views.endpoint_delete, name="endpoint_delete"),
]

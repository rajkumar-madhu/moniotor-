from django.urls import path

from . import views

app_name = "agent"
urlpatterns = [
    path("agent/", views.chat, name="chat"),
    path("agent/new/", views.new_thread, name="new"),
    path("agent/<int:pk>/", views.thread, name="thread"),
    path("agent/<int:pk>/message/", views.send_message, name="message"),
    path("agent/<int:pk>/delete/", views.delete_thread, name="delete"),
]

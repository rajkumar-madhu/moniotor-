from django.db import models
from django.contrib.auth.models import User

class ServiceModel(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.TextField()
    syntax = models.TextField()
    delimiter = models.TextField()
    is_inputRequired = models.BooleanField(default=False)
    is_defaultservice = models.BooleanField(default=False)
    messageformat = models.TextField()

    class Meta:
       db_table= "service"

class UserNotificationSetingsModel(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    service = models.ForeignKey(ServiceModel, on_delete=models.CASCADE)
    inputs = models.TextField()
    url = models.TextField()
    is_saved = models.BooleanField(default=True) 

    class Meta:
       db_table= "user_notification_settings"

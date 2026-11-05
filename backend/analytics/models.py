from django.db import models
from django.contrib.auth.models import User

class UserSettingsModel(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    username = models.CharField(max_length=250)
    analytics = models.CharField(max_length=750)
    jsonid = models.IntegerField()

    class Meta:
       db_table= "usersettings"
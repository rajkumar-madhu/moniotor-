from django.db import models
from django.contrib.auth.models import Group, User
from applications.models import ApplicationModel
from lesites.models import SiteModel

# Create your models here.
class PermissionsModel(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.TextField()
    codename = models.TextField()
    class Meta:
       db_table="permissions"

class Userapplication(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    application = models.ForeignKey(ApplicationModel, on_delete=models.CASCADE)
    weightage = models.IntegerField(default=None, blank=True, null=True)
    class Meta:
       db_table="user_applications"

Group.add_to_class('weightage', models.IntegerField(blank=True, default=16))  #16 = 10000 ['VA']

class Usersite(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    site = models.ForeignKey(SiteModel, on_delete=models.CASCADE)
    is_enable = models.BooleanField(default=True)
    class Meta:
       db_table="user_sites"

class Userotp(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    otp = models.IntegerField(default=000000)
    created_at = models.DateTimeField(auto_now=True)
    class Meta:
       db_table="user_otp"

class UserTOTP(models.Model):
    """Model to store Google Authenticator TOTP secrets for users"""
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    secret_key = models.CharField(max_length=32)  # Base32 encoded secret
    is_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
    class Meta:
       db_table="user_totp"

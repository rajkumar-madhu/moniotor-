from django.db import models
from django.contrib.auth.models import User
from lesites.models import SiteModel


class policynotifiModel(models.Model):
    policy_id = models.AutoField(primary_key=True)
    subject_category = models.CharField(max_length=50)
    device_type = models.CharField(max_length=50)
    device_friendly_name = models.CharField(max_length=255)
    device_ip = models.CharField(max_length=100)
    categories = models.TextField()  # Hardware or Hardware,Software,Ping
    escalation_mails = models.TextField()
    definite_mails = models.TextField()
    approval_timer = models.IntegerField(default=0)
    resolution_timer = models.IntegerField(default=0)
    escalation_required = models.BooleanField(default=True)
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "policy"


class subsiteModel(models.Model):
    id = models.AutoField(primary_key=True)
    site = models.ForeignKey(SiteModel, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    sub_site = models.TextField()

    class Meta:
        db_table = "subsite"

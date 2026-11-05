from django.db import models

class AuditlogsModel(models.Model):
    id = models.AutoField(primary_key=True)
    created = models.DateTimeField(auto_now_add=True)
    username = models.TextField()
    action = models.TextField()
    status = models.CharField(max_length=60)
    message = models.TextField()

    class Meta:
       db_table= "auditlogs"
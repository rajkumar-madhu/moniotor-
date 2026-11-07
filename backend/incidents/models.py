from django.db import models

class IncidentModel(models.Model):
    INCIDENT_PRIORITIES = [
        ('P1', 'P1 - Critical'),
        ('P2', 'P2 - High'),
        ('P3', 'P3 - Medium'),
        ('P4', 'P4 - Low'),
    ]
    
    INCIDENT_STATES = [
        ('NEW', 'New'),
        ('IN_PROGRESS', 'In Progress'),
        ('ON_HOLD', 'On Hold'),
        ('ESCALATED', 'Escalated'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED', 'Closed'),
    ]
    
    id = models.AutoField(primary_key=True)
    number = models.CharField(max_length=20, unique=True)
    short_description = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    priority = models.CharField(max_length=2, choices=INCIDENT_PRIORITIES, default='P3')
    state = models.CharField(max_length=20, choices=INCIDENT_STATES, default='NEW')
    assigned_to_first_name = models.CharField(max_length=100, blank=True, null=True)
    assigned_to_last_name = models.CharField(max_length=100, blank=True, null=True)
    site_name = models.CharField(max_length=100, default='default')
    sla_breached = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(blank=True, null=True)
    
    class Meta:
        db_table = 'incidents'
        verbose_name = 'Incident'
        verbose_name_plural = 'Incidents'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.number} - {self.short_description}"
    
    @property
    def assigned_to(self):
        if self.assigned_to_first_name and self.assigned_to_last_name:
            return f"{self.assigned_to_first_name} {self.assigned_to_last_name}"
        return None

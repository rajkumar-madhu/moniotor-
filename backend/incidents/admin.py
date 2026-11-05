from django.contrib import admin
from .models import IncidentModel

@admin.register(IncidentModel)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ('number', 'short_description', 'priority', 'state', 'assigned_to_first_name', 'assigned_to_last_name', 'site_name', 'created_at')
    list_filter = ('priority', 'state', 'site_name', 'created_at')
    search_fields = ('number', 'short_description', 'description')
    list_editable = ('state', 'priority', 'assigned_to_first_name', 'assigned_to_last_name')
    readonly_fields = ('number', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('number', 'short_description', 'description')
        }),
        ('Classification', {
            'fields': ('priority', 'state')
        }),
        ('Assignment', {
            'fields': ('assigned_to_first_name', 'assigned_to_last_name')
        }),
        ('System', {
            'fields': ('site_name', 'sla_breached', 'created_at', 'updated_at', 'resolved_at'),
            'classes': ('collapse',)
        }),
    )

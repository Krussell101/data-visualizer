from django.contrib import admin
from .models import Dataset, AnalysisSession, QueryLog


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'status', 'uploaded_at']
    list_filter = ['status', 'uploaded_at']
    search_fields = ['name', 'user__username']
    readonly_fields = ['id', 'uploaded_at', 'metadata']


@admin.register(AnalysisSession)
class AnalysisSessionAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'dataset', 'created_at', 'updated_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['title', 'user__username', 'dataset__name']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(QueryLog)
class QueryLogAdmin(admin.ModelAdmin):
    list_display = ['session', 'prompt_preview', 'status', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['prompt', 'response_text']
    readonly_fields = ['id', 'created_at']

    def prompt_preview(self, obj):
        """Show first 50 characters of prompt."""
        return obj.prompt[:50] + '...' if len(obj.prompt) > 50 else obj.prompt

    prompt_preview.short_description = 'Prompt'

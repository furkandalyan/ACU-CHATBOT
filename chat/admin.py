from django.contrib import admin
from .models import ChatSession, ChatMessage, UniversityContent, Faculty, Department, Course, ScraperLog


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display   = ('session_id', 'created_at')
    ordering       = ('-created_at',)
    readonly_fields = ('session_id', 'created_at')


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display   = ('session', 'get_question_preview', 'created_at')
    search_fields  = ('question', 'answer')
    ordering       = ('-created_at',)

    def get_question_preview(self, obj):
        return obj.question[:60]
    get_question_preview.short_description = 'Soru'


@admin.register(UniversityContent)
class UniversityContentAdmin(admin.ModelAdmin):
    list_display    = ('title', 'category', 'language', 'is_active', 'scraped_at')
    list_filter     = ('category', 'language', 'is_active')
    search_fields   = ('title', 'content', 'url')
    ordering        = ('-scraped_at',)
    list_editable   = ('is_active',)
    readonly_fields = ('scraped_at', 'updated_at')


@admin.register(Faculty)
class FacultyAdmin(admin.ModelAdmin):
    list_display  = ('name', 'short_name', 'created_at')
    search_fields = ('name', 'short_name')


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display  = ('name', 'faculty', 'created_at')
    list_filter   = ('faculty',)
    search_fields = ('name',)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display  = ('code', 'name', 'department', 'credits', 'semester')
    list_filter   = ('department__faculty', 'semester')
    search_fields = ('code', 'name', 'description')


@admin.register(ScraperLog)
class ScraperLogAdmin(admin.ModelAdmin):
    list_display    = ('url', 'status', 'records_saved', 'duration_seconds', 'started_at')
    list_filter     = ('status',)
    ordering        = ('-started_at',)
    readonly_fields = ('started_at',)

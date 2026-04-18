from django.contrib import admin
from .models import ChatSession, ChatMessage, UniversityContent


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ['session_id', 'created_at']
    ordering = ['-created_at']


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['get_question_preview', 'get_answer_preview', 'created_at']
    search_fields = ['question', 'answer']
    ordering = ['-created_at']

    def get_question_preview(self, obj):
        return obj.question[:50]
    get_question_preview.short_description = 'Soru'

    def get_answer_preview(self, obj):
        return obj.answer[:50]
    get_answer_preview.short_description = 'Cevap'


@admin.register(UniversityContent)
class UniversityContentAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'url', 'created_at']
    search_fields = ['title', 'content']
    list_filter = ['category']
    ordering = ['-created_at']
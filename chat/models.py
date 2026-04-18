from django.db import models
import uuid


class ChatSession(models.Model):
    session_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Session {self.session_id} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"

    class Meta:
        ordering = ['-created_at']


class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    question = models.TextField()
    answer = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.question[:50]}..."

    class Meta:
        ordering = ['created_at']


class UniversityContent(models.Model):
    title = models.CharField(max_length=500)
    content = models.TextField()
    url = models.URLField(max_length=1000, blank=True)
    category = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ['-created_at']
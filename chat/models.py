from django.db import models
import uuid


class UserProfile(models.Model):
    USER_TYPE_CHOICES = [
        ('student', 'Öğrenci'),
        ('guest', 'Misafir'),
    ]
    name = models.CharField(max_length=200)
    student_number = models.CharField(max_length=20, blank=True, null=True, unique=True)
    email = models.EmailField(blank=True, null=True)
    user_type = models.CharField(max_length=10, choices=USER_TYPE_CHOICES, default='guest')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.user_type})"

    class Meta:
        ordering = ['-created_at']


class ChatSession(models.Model):
    session_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Session {self.session_id}"

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
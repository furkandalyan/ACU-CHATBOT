from django.db import models
import uuid


# ─────────────────────────────────────────────
# Chat oturumu ve mesajlar
# ─────────────────────────────────────────────

class ChatSession(models.Model):
    session_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Session {self.session_id} - {self.created_at.strftime('%d/%m/%Y %H:%M')}"

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Chat Oturumu"
        verbose_name_plural = "Chat Oturumları"


class ChatMessage(models.Model):
    session = models.ForeignKey(
        ChatSession, on_delete=models.CASCADE, related_name='messages'
    )
    question = models.TextField()
    answer   = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.question[:50]}..."

    class Meta:
        ordering = ['created_at']
        verbose_name = "Chat Mesajı"
        verbose_name_plural = "Chat Mesajları"


# ─────────────────────────────────────────────
# Üniversite içerikleri (scraper tarafından doldurulur)
# ─────────────────────────────────────────────

class UniversityContent(models.Model):
    """
    Web scraper'dan gelen ham içerik.
    Her satır bir sayfaya veya sayfa bölümüne karşılık gelir.
    """

    CATEGORY_CHOICES = [
        ('academic',   'Akademik Programlar'),
        ('course',     'Dersler'),
        ('campus',     'Kampüs & Tesis'),
        ('faculty',    'Fakülte & Bölümler'),
        ('admission',  'Kayıt & Kabul'),
        ('research',   'Araştırma'),
        ('news',       'Haberler & Duyurular'),
        ('contact',    'İletişim'),
        ('other',      'Diğer'),
    ]

    title    = models.CharField(max_length=500)
    content  = models.TextField()
    url      = models.URLField(max_length=1000, blank=True)
    category = models.CharField(
        max_length=50, choices=CATEGORY_CHOICES, default='other', db_index=True
    )
    language = models.CharField(max_length=10, default='tr')  # 'tr' veya 'en'
    scraped_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    is_active   = models.BooleanField(default=True)

    class Meta:
        ordering = ['-scraped_at']
        verbose_name = "Üniversite İçeriği"
        verbose_name_plural = "Üniversite İçerikleri"
        indexes = [
            models.Index(fields=['category']),
            models.Index(fields=['language']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"[{self.get_category_display()}] {self.title}"


# ─────────────────────────────────────────────
# Fakülte / Bölüm tablosu
# ─────────────────────────────────────────────

class Faculty(models.Model):
    name        = models.CharField(max_length=300, unique=True)
    short_name  = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    url         = models.URLField(max_length=1000, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Fakülte"
        verbose_name_plural = "Fakülteler"
        ordering = ['name']

    def __str__(self):
        return self.name


class Department(models.Model):
    faculty     = models.ForeignKey(
        Faculty, on_delete=models.CASCADE, related_name='departments'
    )
    name        = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    url         = models.URLField(max_length=1000, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Bölüm"
        verbose_name_plural = "Bölümler"
        unique_together = [('faculty', 'name')]
        ordering = ['faculty', 'name']

    def __str__(self):
        return f"{self.faculty.short_name or self.faculty.name} – {self.name}"


# ─────────────────────────────────────────────
# Ders kataloğu
# ─────────────────────────────────────────────

class Course(models.Model):
    department  = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name='courses', null=True, blank=True
    )
    code        = models.CharField(max_length=20, blank=True)
    name        = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    credits     = models.PositiveSmallIntegerField(null=True, blank=True)
    semester    = models.CharField(max_length=20, blank=True)  # örn. "Güz", "Bahar"
    url         = models.URLField(max_length=1000, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ders"
        verbose_name_plural = "Dersler"
        ordering = ['code', 'name']

    def __str__(self):
        return f"{self.code} – {self.name}" if self.code else self.name


# ─────────────────────────────────────────────
# Scraper çalışma günlüğü
# ─────────────────────────────────────────────

class ScraperLog(models.Model):
    STATUS_CHOICES = [
        ('success', 'Başarılı'),
        ('partial', 'Kısmen Başarılı'),
        ('failed',  'Başarısız'),
    ]

    url         = models.URLField(max_length=1000)
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES)
    records_saved = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at  = models.DateTimeField(auto_now_add=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name = "Scraper Günlüğü"
        verbose_name_plural = "Scraper Günlükleri"

    def __str__(self):
        return f"[{self.status}] {self.url} ({self.started_at.strftime('%d/%m/%Y %H:%M')})"
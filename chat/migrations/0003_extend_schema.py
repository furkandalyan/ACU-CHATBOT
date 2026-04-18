# Generated manually – 2026-04-19
# Extends the initial schema with Faculty, Department, Course and ScraperLog tables.
# Also adds category choices, language, is_active, updated_at to UniversityContent.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0001_initial"),
    ]

    operations = [
        # ── UniversityContent: yeni alanlar ──────────────────────────────────
        migrations.AddField(
            model_name="universitycontent",
            name="language",
            field=models.CharField(default="tr", max_length=10),
        ),
        migrations.AddField(
            model_name="universitycontent",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="universitycontent",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name="universitycontent",
            name="category",
            field=models.CharField(
                choices=[
                    ("academic",  "Akademik Programlar"),
                    ("course",    "Dersler"),
                    ("campus",    "Kampüs & Tesis"),
                    ("faculty",   "Fakülte & Bölümler"),
                    ("admission", "Kayıt & Kabul"),
                    ("research",  "Araştırma"),
                    ("news",      "Haberler & Duyurular"),
                    ("contact",   "İletişim"),
                    ("other",     "Diğer"),
                ],
                db_index=True,
                default="other",
                max_length=50,
            ),
        ),
        # Rename created_at → scraped_at (new field approach for simplicity)
        migrations.RenameField(
            model_name="universitycontent",
            old_name="created_at",
            new_name="scraped_at",
        ),
        # ── Faculty ──────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Faculty",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=300, unique=True)),
                ("short_name", models.CharField(blank=True, max_length=50)),
                ("description", models.TextField(blank=True)),
                ("url", models.URLField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Fakülte",
                "verbose_name_plural": "Fakülteler",
                "ordering": ["name"],
            },
        ),
        # ── Department ───────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Department",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("faculty", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="departments", to="chat.faculty")),
                ("name", models.CharField(max_length=300)),
                ("description", models.TextField(blank=True)),
                ("url", models.URLField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Bölüm",
                "verbose_name_plural": "Bölümler",
                "ordering": ["faculty", "name"],
                "unique_together": {("faculty", "name")},
            },
        ),
        # ── Course ───────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Course",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("department", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="courses", to="chat.department")),
                ("code", models.CharField(blank=True, max_length=20)),
                ("name", models.CharField(max_length=300)),
                ("description", models.TextField(blank=True)),
                ("credits", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("semester", models.CharField(blank=True, max_length=20)),
                ("url", models.URLField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Ders",
                "verbose_name_plural": "Dersler",
                "ordering": ["code", "name"],
            },
        ),
        # ── ScraperLog ───────────────────────────────────────────────────────
        migrations.CreateModel(
            name="ScraperLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("url", models.URLField(max_length=1000)),
                ("status", models.CharField(
                    choices=[
                        ("success", "Başarılı"),
                        ("partial", "Kısmen Başarılı"),
                        ("failed",  "Başarısız"),
                    ],
                    max_length=10,
                )),
                ("records_saved", models.PositiveIntegerField(default=0)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("duration_seconds", models.FloatField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Scraper Günlüğü",
                "verbose_name_plural": "Scraper Günlükleri",
                "ordering": ["-started_at"],
            },
        ),
        # ── Indexes on UniversityContent ─────────────────────────────────────
        migrations.AddIndex(
            model_name="universitycontent",
            index=models.Index(fields=["category"], name="chat_unicontent_category_idx"),
        ),
        migrations.AddIndex(
            model_name="universitycontent",
            index=models.Index(fields=["language"], name="chat_unicontent_language_idx"),
        ),
        migrations.AddIndex(
            model_name="universitycontent",
            index=models.Index(fields=["is_active"], name="chat_unicontent_active_idx"),
        ),
    ]

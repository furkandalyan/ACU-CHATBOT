from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import Course, Department, Faculty, UniversityContent
from .services import answer_question, retrieve_context


class RetrievalTests(TestCase):
    def setUp(self):
        self.faculty = Faculty.objects.create(name="Mühendislik ve Doğa Bilimleri Fakültesi")
        self.department = Department.objects.create(
            faculty=self.faculty,
            name="Bilgisayar Mühendisliği",
            description="Yazılım, algoritma ve yapay zeka alanlarında eğitim verir.",
        )
        Course.objects.create(
            department=self.department,
            code="CSE204",
            name="Veritabanı Sistemleri",
            description="İlişkisel veritabanı tasarımı ve SQL temelleri.",
            credits=3,
            semester="Bahar",
        )
        UniversityContent.objects.create(
            title="Bilgisayar Mühendisliği Programı",
            content="Bilgisayar Mühendisliği programı yazılım geliştirme, veri yapıları ve yapay zeka dersleri içerir.",
            category="academic",
            language="tr",
            url="https://example.com/program",
        )

    def test_retrieve_context_returns_relevant_records(self):
        results = retrieve_context("Bilgisayar mühendisliği programında yapay zeka var mı?")
        self.assertTrue(results)
        self.assertEqual(results[0].title, "Bilgisayar Mühendisliği Programı")


class AnswerQuestionTests(TestCase):
    @override_settings(OLLAMA_URL="http://ollama:11434", OLLAMA_MODEL="mistral", OLLAMA_TIMEOUT=10)
    @patch("chat.services.requests.post")
    def test_answer_question_uses_ollama_and_returns_sources(self, mock_post):
        UniversityContent.objects.create(
            title="Burs Olanakları",
            content="Başarılı öğrencilere burs olanakları sunulur.",
            category="admission",
            language="tr",
            url="https://example.com/burs",
        )

        mock_post.return_value.json.return_value = {"response": "Üniversite çeşitli burs olanakları sunar."}
        mock_post.return_value.raise_for_status.return_value = None

        result = answer_question("Burs var mı?")

        self.assertIn("burs", result["answer"].lower())
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["title"], "Burs Olanakları")

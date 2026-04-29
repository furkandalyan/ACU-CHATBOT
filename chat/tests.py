from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import Department, Faculty, UniversityContent
from .services import answer_question, normalize_text, retrieve_context


class RetrievalTests(TestCase):
    def setUp(self):
        self.faculty = Faculty.objects.create(name="Mühendislik ve Doğa Bilimleri Fakültesi")
        Department.objects.create(
            faculty=self.faculty,
            name="Bilgisayar Mühendisliği (İngilizce)",
            description="Yazılım, algoritma ve yapay zeka alanlarında eğitim verir.",
        )
        UniversityContent.objects.create(
            title="Program Hakkında – Bilgisayar Mühendisliği (İngilizce)",
            content="Bilgisayar Mühendisliği (İngilizce) programı veri bilimi ve yapay zeka odaklıdır.",
            category="academic",
            language="tr",
            url="https://example.com/program",
        )

    def test_normalize_text_handles_real_turkish_characters(self):
        self.assertEqual(
            normalize_text("Bilgisayar Mühendisliği bölüm ücreti"),
            "bilgisayar muhendisligi bolum ucreti",
        )

    def test_retrieve_context_returns_department_match_for_turkish_query(self):
        results = retrieve_context("Bilgisayar Mühendisliği (İngilizce) programı hakkında bilgi verir misin?")
        self.assertTrue(results)
        self.assertIn("Bilgisayar Mühendisliği", results[0].title)


class AnswerQuestionTests(TestCase):
    @override_settings(OLLAMA_URL="http://ollama:11434", OLLAMA_MODEL="mistral", OLLAMA_TIMEOUT=10)
    @patch("chat.services.call_ollama")
    def test_answer_question_calls_llm_with_relevant_context(self, mock_call_ollama):
        mock_call_ollama.return_value = "Ödeme yöntemleri arasında peşin, e-ödeme ve taksit seçenekleri mevcuttur."

        UniversityContent.objects.create(
            title="Ödeme Yöntemleri",
            content=(
                "Eğitim öğretim ücretini peşin ödeme, e-ödeme ve taksitle kredili ödeme "
                "yöntemleri ile yapabilirsiniz. Öğrenim ücreti bir akademik yılın güz ve "
                "bahar dönemlerini kapsar ve dönem başlarında olmak üzere iki eşit tutarda ödenir."
            ),
            category="other",
            language="tr",
            url="https://example.com/payment",
        )

        result = answer_question(
            "Bilgisayar Mühendisliği bölümünün ödeme yöntemleri nelerdir?"
        )

        self.assertTrue(mock_call_ollama.called)
        self.assertIsInstance(result["answer"], str)
        self.assertGreater(len(result["answer"]), 5)

    def test_answer_question_returns_fallback_when_no_context(self):
        result = answer_question("xyzzy123 tamamen alakasız bir konu hakkında bilgi ver")
        self.assertEqual(result["meta"]["strategy"], "fallback")
        self.assertFalse(result["meta"]["cache_hit"])

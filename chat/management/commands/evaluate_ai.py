from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand

from chat.services import answer_question, normalize_text


@dataclass(frozen=True)
class EvaluationCase:
    question: str
    expected_any: tuple[str, ...]
    note: str
    allow_fallback: bool = False


EVALUATION_CASES = [
    EvaluationCase(
        question="Bilgisayar Muhendisligi dersleri neler?",
        expected_any=("CSE", "Programlamaya Giris", "Kalkulus", "Fizik"),
        note="Course list should come from Bologna/course data.",
    ),
    EvaluationCase(
        question="Bilgisayar Muhendisligi bolum baskani kim?",
        expected_any=("baskan", "yetkili", "akademik", "muhendisligi"),
        note="Faculty/staff questions should be grounded in program official data.",
    ),
    EvaluationCase(
        question="Psikoloji programi hakkinda bilgi verir misin?",
        expected_any=("Psikoloji", "program", "fakulte", "lisans"),
        note="Program description should be concise and source-backed.",
    ),
    EvaluationCase(
        question="Burs olanaklari nelerdir?",
        expected_any=("burs", "egitim", "basari", "olanak"),
        note="Admission/finance answers should avoid invented policy details.",
    ),
    EvaluationCase(
        question="Acibadem Universitesi iletisim bilgileri nedir?",
        expected_any=("iletisim", "telefon", "adres", "mail", "e-posta"),
        note="Contact answer should use a contact source or say not enough info.",
    ),
    EvaluationCase(
        question="Kampus olanaklari nelerdir?",
        expected_any=("kampus", "tesis", "yemek", "ulasim", "yasam"),
        note="Campus-life answer should stay within retrieved content.",
    ),
    EvaluationCase(
        question="Eczacilik Fakultesi hakkinda bilgi verir misin?",
        expected_any=("Eczacilik", "fakulte", "program"),
        note="Faculty/program answer should not drift into unrelated departments.",
    ),
    EvaluationCase(
        question="Fizyoterapi ve Rehabilitasyon dersleri neler?",
        expected_any=("Fizyoterapi", "Rehabilitasyon", "ders", "kod"),
        note="Course questions should favor direct DB answers when possible.",
    ),
    EvaluationCase(
        question="Yuksek lisans programlari nelerdir?",
        expected_any=("yuksek lisans", "enstitu", "tezli", "tezsiz"),
        note="Graduate program answer should be based on collected program content.",
    ),
    EvaluationCase(
        question="Oryantasyon hakkinda bilgi verir misin?",
        expected_any=("oryantasyon", "ogrenci", "program"),
        note="If no reliable source exists, fallback is better than hallucination.",
        allow_fallback=True,
    ),
]


class Command(BaseCommand):
    help = "Evaluate chatbot answer quality and latency on a fixed ACU question set."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=len(EVALUATION_CASES),
            help="Number of evaluation cases to run.",
        )
        parser.add_argument(
            "--clear-cache",
            action="store_true",
            help="Clear Django cache before running so timings are cold.",
        )
        parser.add_argument(
            "--max-answer-chars",
            type=int,
            default=220,
            help="Maximum answer preview length printed per case.",
        )
        parser.add_argument(
            "--llm-timeout",
            type=int,
            default=25,
            help="Temporary Ollama timeout in seconds for this evaluation run.",
        )

    def handle(self, *args, **options):
        if options["clear_cache"]:
            cache.clear()
            self.stdout.write("Cache cleared.\n")

        settings.OLLAMA_TIMEOUT = max(1, options["llm_timeout"])

        limit = max(0, min(options["limit"], len(EVALUATION_CASES)))
        max_answer_chars = options["max_answer_chars"]
        cases = EVALUATION_CASES[:limit]

        passed = 0
        warned = 0
        total_ms = 0

        for index, case in enumerate(cases, start=1):
            result = answer_question(case.question)
            answer = result.get("answer", "")
            meta = result.get("meta", {})
            timings = meta.get("timings_ms", {})
            strategy = meta.get("strategy", "unknown")
            context_count = meta.get("context_count", len(result.get("sources", [])))
            elapsed_ms = timings.get("total", 0)
            total_ms += elapsed_ms

            normalized_answer = normalize_text(answer)
            matched_keywords = [
                keyword
                for keyword in case.expected_any
                if normalize_text(keyword) in normalized_answer
            ]
            has_sources = bool(result.get("sources"))
            is_fallback = strategy == "fallback"
            ok = (
                bool(matched_keywords) and has_sources and not is_fallback
            ) or (case.allow_fallback and is_fallback)

            if ok:
                passed += 1
                status = self.style.SUCCESS("PASS")
            else:
                warned += 1
                status = self.style.WARNING("WARN")

            answer_preview = " ".join(answer.split())
            if len(answer_preview) > max_answer_chars:
                answer_preview = answer_preview[:max_answer_chars].rstrip() + "..."

            self.stdout.write(f"{index}. {status} {case.question}")
            self.stdout.write(
                f"   strategy={strategy} cache_hit={meta.get('cache_hit', False)} "
                f"contexts={context_count} total_ms={elapsed_ms} "
                f"retrieve_ms={timings.get('retrieve', '-')} llm_ms={timings.get('llm', '-')}"
            )
            self.stdout.write(
                "   matched="
                + (", ".join(matched_keywords) if matched_keywords else "-")
            )
            self.stdout.write(f"   answer={answer_preview.encode('ascii', 'replace').decode('ascii')}")
            self.stdout.write(f"   note={case.note}\n")

        average_ms = int(total_ms / len(cases)) if cases else 0
        self.stdout.write(
            self.style.SUCCESS(
                f"Summary: pass={passed} warn={warned} total={len(cases)} avg_ms={average_ms}"
            )
        )

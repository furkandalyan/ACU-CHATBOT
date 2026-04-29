import hashlib
import json
import logging
import re
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Generator

import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

from .models import Course, Department, Faculty, UniversityContent


HTTP_SESSION = requests.Session()
logger = logging.getLogger(__name__)

TURKISH_CHAR_MAP = str.maketrans(
    {
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
        "Ç": "c", "Ğ": "g", "İ": "i", "I": "i", "Ö": "o", "Ş": "s", "Ü": "u",
    }
)

STOP_WORDS = {
    "acibadem", "acıbadem", "universitesi", "üniversitesi", "universite",
    "üniversite", "hakkinda", "hakkında", "bilgi", "verir", "misin", "mısın",
    "nedir", "hangi", "olan", "icin", "için", "ile", "ve", "veya", "ama",
    "gibi", "bir", "bu", "da", "de", "mi", "mı", "mu", "mü", "dersleri",
    "dersler", "şartları", "sartlari", "nelerdir", "neler", "nerede",
    "nasıl", "nasil", "var", "programı", "programi",
}

CATEGORY_HINTS = {
    "admission": {"başvuru", "basvuru", "kayıt", "kayit", "kabul", "burs", "ücret", "ucret"},
    "course": {"ders", "müfredat", "mufredat", "course", "akts", "ects", "credit", "kredi"},
    "campus": {"kampüs", "kampus", "yurt", "konaklama", "yemek", "ulaşım", "ulasim"},
    "contact": {"iletişim", "iletisim", "telefon", "mail", "e-posta", "adres"},
    "academic": {"program", "bölüm", "bolum", "lisans", "yüksek lisans", "doktora"},
    "faculty": {
        "fakülte", "fakulte", "bölüm başkanı", "bolum baskani",
        "başkan", "baskan", "dekan", "yetkili", "akademik personel", "kadro",
    },
    "research": {"araştırma", "arastirma", "laboratuvar", "proje", "merkez"},
    "news": {"duyuru", "haber", "etkinlik"},
}

# System prompt — natural, grounded, works across all open-source models
SYSTEM_PROMPT = """Sen Acıbadem Üniversitesi bilgi asistanısın. Sana "Kaynak bilgileri" bölümünde verilen içeriği kullanarak soruları Türkçe yanıtlarsın.

Kesin kurallar:
1. Kaynak bilgileri bölümünde içerik varsa o içeriği kullanarak yanıt ver — "Bu konuda bilgim yok" DEME.
2. Hemen cevaba gir — "Tabii ki", "Yardımcı olabilirim", "Evet yapabilirim" gibi giriş YAZMA.
3. "Bu kaynaklar arasında fark var", "Bu bilgilere dayanarak" gibi meta yorum YAPMA — doğrudan cevabı yaz.
4. Tek kelimeyle ("Evet", "Var", "Hayır") cevap VERME — mutlaka 2-3 tam cümle yaz.
5. Web adresi veya URL yazma.
6. Maksimum 4 cümle yaz."""


@dataclass
class RetrievedChunk:
    title: str
    body: str
    source_type: str
    url: str = ""
    category: str = "other"
    language: str = "tr"
    score: int = 0
    matched_tokens: int = 0


def normalize_text(value: str) -> str:
    value = (value or "").translate(TURKISH_CHAR_MAP).lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def tokenize_query(question: str) -> list[str]:
    normalized = normalize_text(question)
    return [t for t in normalized.split() if len(t) >= 3 and t not in STOP_WORDS]


def expand_query_tokens(question: str, tokens: list[str]) -> list[str]:
    expanded = list(tokens)
    normalized = normalize_text(question)

    if any(token in normalized for token in ["kampus", "nerede", "adres"]):
        expanded.extend(["iletisim", "ulasim", "adres", "kayisdagi", "atasehir"])

    if "burs" in normalized:
        expanded.extend(["burs", "olanaklari", "egitim", "basari"])

    seen: set[str] = set()
    return [t for t in expanded if not (t in seen or seen.add(t))]


def detect_category(question: str) -> str | None:
    normalized = normalize_text(question)
    if any(token in normalized for token in ["kampus nerede", "kampus adres", "nerede"]):
        return "contact"
    if any(hint in normalized for hint in [
        "bölüm başkanı", "bolum baskani", "başkan", "baskan",
        "dekan", "yetkili", "akademik personel", "kadro",
    ]):
        return "faculty"
    for category, hints in CATEGORY_HINTS.items():
        if any(hint in normalized for hint in hints):
            return category
    return None


def expand_question_for_llm(question: str) -> str:
    """
    Rephrase terse or yes/no questions so the LLM gives a full answer
    instead of a one-word reply.
    """
    normalized = normalize_text(question)
    q = question.strip().rstrip("?").strip()

    # Yes/no patterns → ask for explanation
    yn_triggers = ["var mi", "var mı", "mevcut mu", "sunuluyor mu", "yapiliyor mu", "yapılıyor mu"]
    if any(t in normalized for t in yn_triggers):
        # "Burs var mı?" → "Burs olanakları nelerdir?"
        topic = q.rstrip("?").strip()
        # Remove trailing "var mı" / "var mi" fragment
        topic = re.sub(r"\s+var\s+m[ıi]\s*$", "", topic, flags=re.IGNORECASE).strip()
        return f"{topic} olanakları veya seçenekleri nelerdir, kısaca açıkla."

    # "Bilgi verebilir misin?" style → drop the politeness, just ask
    meta_triggers = ["bilgi verebilir misin", "anlatir misin", "anlatır mısın",
                     "soyle", "söyle", "hakkinda bilgi", "hakkında bilgi"]
    if any(t in normalized for t in meta_triggers):
        # Already an informational request, keep as-is
        return question

    # Very short questions (≤ 3 words) that aren't a name → ask to elaborate
    tokens = [w for w in normalized.split() if len(w) > 2]
    if len(tokens) <= 2:
        return f"{q} hakkında kısaca bilgi ver."

    return question


def is_garbage_answer(answer: str, question: str) -> bool:
    """Detect when the LLM produced a meaningless or off-topic response."""
    if not answer or len(answer.strip()) < 20:
        return True
    normalized = normalize_text(answer)
    confusion_phrases = [
        "bu saptanmaz", "veri iceriyorsa", "bilgi sunucu",
        "veritabaniniza basvurmaniz", "genellikle universite",
        "bu durumla ilgili",
        # Generic hallucinations about universities in general instead of ACU
        "dunyadaki", "dunyada universite", "genel olarak universiteler",
        "her universite farkli",
        # Model talking about itself
        "bir yapay zeka", "dil modeli olarak", "egitim verilerim",
    ]
    if any(p in normalized for p in confusion_phrases):
        return True

    # If answer is just "fakat ..." with no real content — starts with adversative
    if normalized.startswith("fakat ") and len(answer.split()) < 25:
        return True

    # Meta-response: model says what it can do instead of doing it
    meta_response_phrases = [
        "aciklayabilirim", "aktarabilirim", "bilgi verebilirim", "anlatabilir",
        "ozetle", "ozet olarak", "goruntuleyelim",
    ]
    if any(p in normalized for p in meta_response_phrases) and len(answer.split()) < 20:
        return True

    return False


def has_pricing_intent(question: str) -> bool:
    normalized = normalize_text(question)
    return any(t in normalized for t in ["ucret", "odeme", "harc", "tuition", "fee", "fees"])


def has_pricing_evidence(text: str) -> bool:
    normalized = normalize_text(text)
    return any(t in normalized for t in [
        "ucret", "odeme", "pesin", "taksit", "kredi", "kontenjan", "puan", "burs",
    ])


def _word_count(text: str, token: str) -> int:
    """Count whole-word occurrences of token in text (prevents 'burs' matching 'reimbursement')."""
    return len(re.findall(r"\b" + re.escape(token) + r"\b", text))


def score_text(text: str, title: str, tokens: list[str]) -> int:
    haystack = normalize_text(text)
    normalized_title = normalize_text(title)
    score = 0

    for token in tokens:
        if _word_count(normalized_title, token):
            score += 5
        occurrences = _word_count(haystack, token)
        if occurrences:
            score += min(occurrences, 5) * 2

    if tokens:
        joined = " ".join(tokens)
        if joined in haystack:
            score += 6
        if joined in normalized_title:
            score += 10

    return score


def count_matched_tokens(text: str, title: str, tokens: list[str]) -> int:
    haystack = normalize_text(text)
    normalized_title = normalize_text(title)
    return sum(1 for t in tokens if _word_count(haystack, t) or _word_count(normalized_title, t))


def title_relevance_bonus(question: str, title: str, category: str | None) -> int:
    normalized_question = normalize_text(question)
    normalized_title = normalize_text(title)

    if any(kw in normalized_question for kw in ["baskan", "yetkili", "dekan"]):
        if normalized_title.startswith("program yetkilileri"):
            return 18
        if normalized_title.startswith("akademik personel"):
            return -6

    if category == "academic" and normalized_title.startswith("program hakkinda"):
        bonus = 0
        program_name = normalized_title.replace("program hakkinda", "", 1).strip()
        if "ingilizce" in program_name and "ingilizce" not in normalized_question:
            bonus -= 4
        program_name = re.sub(r"\bingilizce\b", "", program_name)
        program_name = re.sub(r"\s+", " ", program_name).strip()
        query_tokens = tokenize_query(question)
        if len(query_tokens) == 1 and program_name == query_tokens[0]:
            return bonus + 18
        if query_tokens and program_name.startswith(" ".join(query_tokens[:2])):
            return bonus + 10

    return 0


def trim_text(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_break = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if last_break > int(limit * 0.6):
        return cut[: last_break + 1]
    return cut + "..."


def serialize_sources(contexts: list[RetrievedChunk]) -> list[dict[str, str]]:
    return [
        {
            "title": item.title,
            "url": item.url,
            "category": item.category,
            "source_type": item.source_type,
        }
        for item in contexts
    ]


def retrieve_context(question: str, language: str = "tr", limit: int = 6) -> list[RetrievedChunk]:
    tokens = expand_query_tokens(question, tokenize_query(question))
    category = detect_category(question)
    normalized_question = normalize_text(question)
    chunks: list[RetrievedChunk] = []
    pricing_intent = has_pricing_intent(question)

    content_query = UniversityContent.objects.filter(is_active=True)
    if language:
        content_query = content_query.filter(language=language)
    base_content_query = content_query

    if tokens:
        content_filter = Q()
        for token in tokens:
            content_filter |= Q(title__icontains=token) | Q(content__icontains=token)
        content_query = content_query.filter(content_filter)
        if category:
            filtered_with_category = content_query.filter(category=category)
            if filtered_with_category.exists():
                content_query = filtered_with_category
    elif category:
        content_query = content_query.filter(category=category)

    if not content_query.exists():
        content_query = base_content_query
        if tokens:
            content_filter = Q()
            for token in tokens:
                content_filter |= Q(title__icontains=token) | Q(content__icontains=token)
            content_query = content_query.filter(content_filter)

    for item in content_query[:200]:
        score = score_text(item.content, item.title, tokens)
        matched_tokens = count_matched_tokens(item.content, item.title, tokens)
        if category and item.category == category:
            score += 4
        score += title_relevance_bonus(question, item.title, item.category)
        if score <= 0 and tokens:
            continue
        if len(tokens) >= 2 and matched_tokens < 2 and score < 12:
            continue
        chunks.append(
            RetrievedChunk(
                title=item.title,
                body=trim_text(item.content),
                source_type="university_content",
                url=item.url,
                category=item.category,
                language=item.language,
                score=score or 1,
                matched_tokens=matched_tokens,
            )
        )

    if category == "contact" and any(
        t in normalized_question for t in ["kampus", "nerede", "adres", "iletisim"]
    ):
        for item in base_content_query.filter(category="contact")[:20]:
            score = score_text(item.content, item.title, tokens) + 20
            matched_tokens = count_matched_tokens(item.content, item.title, tokens)
            chunks.append(
                RetrievedChunk(
                    title=item.title,
                    body=trim_text(item.content, 500),
                    source_type="university_content",
                    url=item.url,
                    category=item.category,
                    language=item.language,
                    score=score,
                    matched_tokens=max(matched_tokens, 2),
                )
            )

    if pricing_intent:
        pricing_query = base_content_query.filter(
            Q(category__in=["admission", "other"])
            & (
                Q(title__icontains="ücret") | Q(title__icontains="ucret")
                | Q(title__icontains="ödeme") | Q(title__icontains="odeme")
                | Q(title__icontains="burs") | Q(title__icontains="kontenjan")
                | Q(content__icontains="ücret") | Q(content__icontains="ucret")
                | Q(content__icontains="ödeme") | Q(content__icontains="odeme")
                | Q(content__icontains="burs") | Q(content__icontains="kontenjan")
            )
        )
        for item in pricing_query[:12]:
            score = score_text(item.content, item.title, tokens) + 12
            if has_pricing_evidence(f"{item.title} {item.content}"):
                score += 8
            if item.category == "admission":
                score += 4
            matched_tokens = count_matched_tokens(item.content, item.title, tokens)
            chunks.append(
                RetrievedChunk(
                    title=item.title,
                    body=trim_text(item.content, 500),
                    source_type="university_content",
                    url=item.url,
                    category=item.category,
                    language=item.language,
                    score=max(score, 1),
                    matched_tokens=max(matched_tokens, 1),
                )
            )

    if tokens:
        faculty_filter = Q()
        department_filter = Q()
        course_filter = Q()
        for token in tokens:
            faculty_filter |= Q(name__icontains=token) | Q(description__icontains=token)
            department_filter |= Q(name__icontains=token) | Q(description__icontains=token)
            course_filter |= (
                Q(code__icontains=token)
                | Q(name__icontains=token)
                | Q(description__icontains=token)
                | Q(semester__icontains=token)
            )

        for faculty in Faculty.objects.filter(faculty_filter)[:10]:
            body = faculty.description or f"{faculty.name} hakkında kayıt bulundu."
            matched_tokens = count_matched_tokens(body, faculty.name, tokens)
            score = score_text(body, faculty.name, tokens) + 3
            if len(tokens) >= 2 and matched_tokens < 2 and score < 12:
                continue
            chunks.append(
                RetrievedChunk(
                    title=faculty.name,
                    body=trim_text(body),
                    source_type="faculty",
                    url=faculty.url,
                    category="faculty",
                    score=score,
                    matched_tokens=matched_tokens,
                )
            )

        for department in Department.objects.select_related("faculty").filter(department_filter)[:10]:
            body = department.description or f"{department.name}, {department.faculty.name} bünyesindedir."
            matched_tokens = count_matched_tokens(body, department.name, tokens)
            score = score_text(body, department.name, tokens) + 4
            if len(tokens) >= 2 and matched_tokens < 2 and score < 12:
                continue
            chunks.append(
                RetrievedChunk(
                    title=f"{department.faculty.name} / {department.name}",
                    body=trim_text(body),
                    source_type="department",
                    url=department.url,
                    category="academic",
                    score=score,
                    matched_tokens=matched_tokens,
                )
            )

        for course in Course.objects.select_related("department", "department__faculty").filter(course_filter)[:12]:
            dept_name = course.department.name if course.department else "Bölüm belirtilmemiş"
            faculty_name = (
                course.department.faculty.name
                if course.department and course.department.faculty
                else "Fakülte belirtilmemiş"
            )
            details = [
                course.code,
                course.name,
                f"Bölüm: {dept_name}",
                f"Fakülte: {faculty_name}",
                f"Kredi: {course.credits}" if course.credits else "",
                f"Dönem: {course.semester}" if course.semester else "",
                course.description,
            ]
            body = " | ".join(p for p in details if p)
            matched_tokens = count_matched_tokens(body, course.name, tokens)
            score = score_text(body, course.name, tokens) + 5
            if len(tokens) >= 2 and matched_tokens < 2 and score < 12:
                continue
            chunks.append(
                RetrievedChunk(
                    title=f"{course.code} {course.name}".strip(),
                    body=trim_text(body),
                    source_type="course",
                    url=course.url,
                    category="course",
                    score=score,
                    matched_tokens=matched_tokens,
                )
            )

    deduped: list[RetrievedChunk] = []
    seen_keys: set[tuple[str, str]] = set()
    for chunk in sorted(chunks, key=lambda c: c.score, reverse=True):
        key = (chunk.title, chunk.url or chunk.body[:80])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(chunk)
        if len(deduped) >= limit:
            break

    return deduped


def _clean_for_llm(text: str) -> str:
    """Strip URLs and artifacts from source content before sending to LLM."""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def build_context_blocks(contexts: list[RetrievedChunk]) -> str:
    if not contexts:
        return "İlgili bilgi bulunamadı."
    blocks = []
    for item in contexts:
        body = _clean_for_llm(item.body)
        block = f"### {item.title}\n{body}"
        blocks.append(block)
    return "\n\n".join(blocks)


def build_fallback_answer(contexts: list[RetrievedChunk]) -> str:
    if not contexts:
        return (
            "Bu konuda veritabanımda yeterli bilgi bulamadım. "
            "Daha spesifik bölüm, ders, fakülte veya başvuru konusu belirtirsen tekrar deneyebilirim."
        )
    return (
        "Bu konuda yeterli ve doğrudan bilgi bulamadım. "
        "Daha spesifik bir bölüm, fakülte veya konu belirtirsen tekrar deneyebilirim."
    )


def sanitize_answer(text: str) -> str:
    """Remove LLM output artifacts that leak prompt internals or look unprofessional."""
    text = re.sub(r"\[Kaynak\s*\d+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Kaynak\s*\d+\s*:", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(kaynak_tipi|kategori|url)=\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?INST\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\|.*?\|>", "", text)
    text = re.sub(r"Kaynak bilgileri:.*?Soru:", "", text, flags=re.DOTALL)
    text = re.sub(r"https?://\S+", "", text)

    # Strip small-model meta-commentary preambles
    meta_prefixes = [
        r"Bu kaynaklar aras[ıi]nda (biraz )?fark var[,.]?\s*(bu bilgilere dayan[ae]rak)?\s*",
        r"(Bu|Verilen|Bilgiye) bilgilere dayan[ae]rak[,.]?\s*",
        r"Bilgiye dayan[ae]rak[,.]?\s*",
        r"Verilen kaynaklara g[öo]re[,.]?\s*",
        r"Kaynak bilgi(?:leri)? (?:incelendi[gğ]inde|do[gğ]rultusunda)[,.]?\s*",
        r"Evet[,.]?\s+(?=\w)",   # leading "Evet, " before actual answer
    ]
    for pattern in meta_prefixes:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Remove inline source-comparison labels ("İlk kaynak X, ikinci kaynak Y")
    text = re.sub(r"(?:[İi]lk|[Bb]irinci|[İi]kinci|[Üü][çc][üu]nc[üu])\s+kaynak\w*[:\s]+", "", text)

    # Strip trailing single-word non-answers (e.g. dangling "Var." at end)
    text = re.sub(r"\s+(?:Var|Yok|Evet|Hayır|Tamam)[.!]?\s*$", "", text, flags=re.IGNORECASE)

    # Strip trailing helper-bot closings
    text = re.sub(
        r"\s*[oO]\s+(?:ornek\s+)?kaynaklarımda?\s+verilen\s+bilgilerle\s+size\s+yardımcı\s+olabilirim[.!]?\s*$",
        "", text,
    )
    text = re.sub(r"\s*[Ss]ize\s+(?:daha\s+fazla\s+)?yard[ıi]mc[ıi]\s+olabilirim[.!]?\s*$", "", text)
    text = re.sub(r"\s*[Yy]ard[ıi]mc[ıi]\s+olabilirim[,.]?\s*(?:daha\s+fazla\s+detay\s+i[çc]in.*)?$", "", text)
    text = re.sub(r"\s*acaba\s+bu\s+kaynaklar\s+alt[ıi]nda\s+bulunmaktad[ıi]r[.!]?\s*$", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s{2,}", " ", text).strip()
    # Re-capitalize first letter if stripping removed a capital start
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def build_result(
    *,
    answer: str,
    contexts: list[RetrievedChunk],
    strategy: str,
    timings_ms: dict[str, int],
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        "answer": answer,
        "sources": serialize_sources(contexts),
        "prompt": "",
        "meta": {
            "strategy": strategy,
            "cache_hit": cache_hit,
            "context_count": len(contexts),
            "timings_ms": timings_ms,
        },
    }


def call_ollama(
    question: str,
    contexts: list[RetrievedChunk],
    conversation_history: list[dict] | None = None,
    max_tokens: int = 450,
    temperature: float = 0.6,
) -> str:
    """
    Call Ollama /api/chat — works with any model (Mistral, LLaMA, Qwen, Gemma, Phi…)
    without model-specific prompt templates.
    """
    context_text = build_context_blocks(contexts)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Last 3 conversation turns give the model conversational context
    if conversation_history:
        for msg in conversation_history[-6:]:
            messages.append({"role": "user", "content": msg["question"]})
            messages.append({"role": "assistant", "content": msg["answer"]})

    messages.append({
        "role": "user",
        "content": f"Kaynak bilgileri:\n{context_text}\n\nSoru: {question}",
    })

    response = HTTP_SESSION.post(
        f"{settings.OLLAMA_URL}/api/chat",
        json={
            "model": settings.OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
            },
        },
        timeout=settings.OLLAMA_TIMEOUT,
    )
    response.raise_for_status()
    return (response.json().get("message", {}).get("content") or "").strip()


def stream_ollama(
    question: str,
    contexts: list[RetrievedChunk],
    conversation_history: list[dict] | None = None,
    max_tokens: int = 220,
    temperature: float = 0.6,
) -> Generator[str, None, None]:
    """Stream tokens from Ollama as they are generated — yields each token string."""
    context_text = build_context_blocks(contexts)
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if conversation_history:
        for msg in conversation_history[-6:]:
            messages.append({"role": "user", "content": msg["question"]})
            messages.append({"role": "assistant", "content": msg["answer"]})

    messages.append({
        "role": "user",
        "content": f"Kaynak bilgileri:\n{context_text}\n\nSoru: {question}",
    })

    with HTTP_SESSION.post(
        f"{settings.OLLAMA_URL}/api/chat",
        json={
            "model": settings.OLLAMA_MODEL,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
            },
        },
        stream=True,
        timeout=settings.OLLAMA_TIMEOUT,
    ) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            chunk = json.loads(raw_line)
            token = chunk.get("message", {}).get("content", "")
            if token:
                yield token
            if chunk.get("done"):
                break


def answer_question(
    question: str,
    language: str = "tr",
    conversation_history: list[dict] | None = None,
) -> dict[str, Any]:
    started_at = perf_counter()

    normalized_cache_input = normalize_text(question)
    cache_digest = hashlib.sha1(
        f"{language}:{normalized_cache_input}".encode("utf-8")
    ).hexdigest()
    cache_key = f"chat:answer:v7:{cache_digest}"

    cached = cache.get(cache_key)
    if cached is not None:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        cached.setdefault("meta", {})
        cached["meta"].update({"cache_hit": True, "timings_ms": {"total": elapsed_ms}})
        logger.info("answer_question cache_hit=true question=%r", question[:120])
        return cached

    retrieve_start = perf_counter()
    contexts = retrieve_context(question=question, language=language, limit=6)
    retrieve_ms = int((perf_counter() - retrieve_start) * 1000)

    top_score = max((c.score for c in contexts), default=0)
    if not contexts or top_score < 6:
        answer = build_fallback_answer(contexts)
        total_ms = int((perf_counter() - started_at) * 1000)
        result = build_result(
            answer=answer,
            contexts=contexts,
            strategy="fallback",
            timings_ms={"retrieve": retrieve_ms, "total": total_ms},
            cache_hit=False,
        )
        cache.set(cache_key, result, 300)
        logger.info(
            "answer_question strategy=fallback retrieve_ms=%s total_ms=%s question=%r",
            retrieve_ms, total_ms, question[:120],
        )
        return result

    # Pass top 2 contexts — drop any whose body is all URLs with no real text
    generation_contexts = [c for c in contexts[:2] if len(_clean_for_llm(c.body)) >= 30]
    if not generation_contexts:
        answer = build_fallback_answer(contexts)
        total_ms = int((perf_counter() - started_at) * 1000)
        result = build_result(
            answer=answer,
            contexts=contexts,
            strategy="fallback",
            timings_ms={"retrieve": retrieve_ms, "total": total_ms},
            cache_hit=False,
        )
        cache.set(cache_key, result, 300)
        return result

    word_count = len(normalized_cache_input.split())
    max_tokens = 120 if word_count <= 5 else 160

    # Expand terse yes/no or vague questions so the model gives full answers
    effective_q = expand_question_for_llm(question)

    llm_start = perf_counter()
    strategy = "llm"
    try:
        answer = call_ollama(
            question=effective_q,
            contexts=generation_contexts,
            conversation_history=conversation_history,
            max_tokens=max_tokens,
            temperature=0.6,
        )
        answer = sanitize_answer(answer)
        if not answer or is_garbage_answer(answer, question):
            answer = build_fallback_answer(contexts)
            strategy = "fallback"

    except requests.exceptions.ConnectionError:
        answer = "Şu anda AI servisine ulaşılamıyor. Ollama çalışıyor mu? Lütfen daha sonra tekrar deneyin."
        strategy = "error"
    except requests.exceptions.Timeout:
        answer = "AI yanıt vermekte geç kaldı. Lütfen tekrar deneyin."
        strategy = "error"
    except Exception as e:
        logger.error("Ollama error: %s", e, exc_info=True)
        answer = "Şu anda cevap üretemiyorum. Lütfen daha sonra tekrar deneyin."
        strategy = "error"

    llm_ms = int((perf_counter() - llm_start) * 1000)
    total_ms = int((perf_counter() - started_at) * 1000)

    result = build_result(
        answer=answer,
        contexts=contexts,
        strategy=strategy,
        timings_ms={"retrieve": retrieve_ms, "llm": llm_ms, "total": total_ms},
        cache_hit=False,
    )
    cache.set(cache_key, result, 300)
    logger.info(
        "answer_question strategy=%s retrieve_ms=%s llm_ms=%s total_ms=%s question=%r",
        strategy, retrieve_ms, llm_ms, total_ms, question[:120],
    )
    return result

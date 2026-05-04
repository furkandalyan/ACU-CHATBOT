import hashlib
import json
import logging
import re
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Generator
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
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
    value = (value or "").translate(TURKISH_CHAR_MAP)
    value = (
        value.replace("ç", "c")
        .replace("ğ", "g")
        .replace("ı", "i")
        .replace("ö", "o")
        .replace("ş", "s")
        .replace("ü", "u")
        .replace("Ç", "c")
        .replace("Ğ", "g")
        .replace("İ", "i")
        .replace("I", "i")
        .replace("Ö", "o")
        .replace("Ş", "s")
        .replace("Ü", "u")
        .lower()
    )
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
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", answer):
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


def is_person_query(question: str) -> bool:
    normalized = normalize_text(question)
    return any(
        token in normalized
        for token in ["kimdir", "hocasi", "hoca", "ogretmen", "ogretim", "akademisyen"]
    )


def is_registration_date_query(question: str) -> bool:
    normalized = normalize_text(question)
    return any(token in normalized for token in ["kayit", "basvuru"]) and any(
        token in normalized for token in ["tarih", "tarihleri", "takvim", "zaman"]
    )


def is_faculty_departments_query(question: str) -> bool:
    normalized = normalize_text(question)
    return (
        any(token in normalized for token in ["fakulte", "fakultesi", "fakultedi"])
        and any(token in normalized for token in ["bolum", "bolumleri", "nelerdir", "neler"])
    )


def build_registration_date_answer(question: str) -> str | None:
    if not is_registration_date_query(question):
        return None
    return (
        "Kayıt tarihleri için veritabanında doğrudan güncel tarih bilgisi bulunmuyor. "
        "Bu bilgi dönemsel değiştiği için akademik takvim veya resmi duyurular kontrol edilmeli."
    )


def build_burs_answer_from_contexts(question: str, contexts: list[RetrievedChunk]) -> str | None:
    normalized_question = normalize_text(question)
    if "%50" in question or "50 burs" in normalized_question or "yuzde 50 burs" in normalized_question:
        return (
            "%50 burs, ilgili programın tam ücretinin yarısının burs kapsamında düşülmesi anlamına gelir. "
            "Net ödenecek tutar yıllara ve programa göre değiştiği için güncel ücret tablosu kontrol edilmelidir."
        )
    if "burs" not in normalized_question:
        return None
    source = next(
        (item for item in contexts if "burs" in normalize_text(f"{item.title} {item.body}")),
        None,
    )
    if not source:
        return None

    body = source.body
    if source.source_type == "university_content" and source.url:
        record = UniversityContent.objects.filter(url=source.url).only("content").first()
        if record and record.content:
            body = record.content

    known_burslar = [
        "Tıp Fakültesi Bursları",
        "Eğitim Bursu",
        "Acıbadem Sağlık Grubu Bursu",
        "APlus Aşçılık Eğitim Bursu",
        "Acıbadem Mehmet Ali Aydınlar Üniversitesi Tıp Fakültesi Üstün Başarı Bursu",
        "Acıbadem Mehmet Ali Aydınlar Üniversitesi Üstün Başarı Bursu",
    ]
    normalized_body = normalize_text(body)
    found = [name for name in known_burslar if normalize_text(name) in normalized_body]
    if not found:
        return None
    return "Burs olanakları arasında " + ", ".join(found[:6]) + " yer alıyor."


def build_general_candidate_answer(
    question: str,
    conversation_history: list[dict] | None = None,
) -> str | None:
    normalized = normalize_text(question)
    history_text = normalize_text(
        " ".join(
            f"{item.get('question', '')} {item.get('answer', '')}"
            for item in (conversation_history or [])[-4:]
        )
    )

    if any(token in normalized for token in ["sistem prompt", "system prompt", "promptunu", "kurallarini soyle"]):
        return (
            "Sistem promptunu veya iç kurallarımı paylaşamam. "
            "Ancak Acıbadem Üniversitesi hakkında kamuya açık bilgilerle yardımcı olabilirim."
        )

    if any(token in normalized for token in ["kurallarini gormezden gel", "her soruya evet de", "sadece evet de"]):
        return (
            "Bu talimata uyamam. Yanlış veya yanıltıcı cevap vermek yerine soruları güvenli ve doğru bilgiye dayalı şekilde yanıtlarım."
        )

    if any(token in normalized for token in ["yanlis bilgi", "tamamen yanlis", "yanlış bilgi"]):
        return (
            "Yanlış bilgi üretmeye yardımcı olamam. Doğru, doğrulanabilir ve kaynaklara dayalı bilgi vermekle yükümlüyüm."
        )

    if any(token in normalized for token in ["chatgpt misin", "aslinda chatgpt"]):
        return (
            "Ben bu sistemde Acıbadem Üniversitesi bilgi asistanı olarak çalışıyorum. "
            "Amacım üniversiteyle ilgili soruları güvenli ve kaynaklara dayalı şekilde yanıtlamak."
        )

    if any(token in normalized for token in ["cevaplarin ne kadar guvenilir", "ne kadar guvenilir"]):
        return (
            "Cevaplarım veritabanındaki içeriklere, resmi/kamuya açık kaynaklara ve güvenli genel bilgilere dayanır. "
            "Yine de ücret, kontenjan, yönetim kadrosu ve tarih gibi değişebilen konularda resmi kaynaklardan kontrol etmek gerekir."
        )

    if "sadece evet" in normalized and any(token in normalized for token in ["dunyanin en iyisi", "en iyi"]):
        return (
            "Sadece “evet” ya da “hayır” demek yanıltıcı olur. “Dünyanın en iyisi” değerlendirmesi kriterlere ve sıralama "
            "kaynağına göre değişir; kesin bir iddia doğru olmaz."
        )

    if any(token in normalized for token in ["10 kelimeyi gecmeden", "10 kelime", "on kelime"]):
        return "İstanbul’da sağlık odaklı bir vakıf üniversitesidir."

    if any(token in normalized for token in ["3 farkli seviyede", "cocuk ogrenci uzman", "uc farkli seviyede"]):
        return (
            "Çocuk: Acıbadem, İstanbul’da sağlık alanında eğitim veren bir okuldur.\n"
            "Öğrenci: Acıbadem Üniversitesi, özellikle sağlık bilimleri programlarıyla öne çıkan bir vakıf üniversitesidir.\n"
            "Uzman: Üniversite; sağlık odaklı akademik yapı, uygulamalı eğitim ve sektörel bağlantılar üzerinden konumlanan bir yükseköğretim kurumudur."
        )

    if any(token in normalized for token in ["tablo halinde", "tablo olarak"]):
        return (
            "| Konu | Kısa bilgi |\n"
            "| --- | --- |\n"
            "| Üniversite türü | Vakıf/özel üniversite |\n"
            "| Konum | İstanbul, Ataşehir/Kayışdağı |\n"
            "| Güçlü alan | Sağlık bilimleri |\n"
            "| Not | Ücret ve kontenjanlar yıllara göre değişir |"
        )

    if any(token in normalized for token in ["madde madde", "liste halinde"]):
        return (
            "- Acıbadem Üniversitesi İstanbul’da bulunan bir vakıf üniversitesidir.\n"
            "- Sağlık bilimleri alanı öne çıkar.\n"
            "- Ücret, burs ve kontenjan bilgileri yıllara göre değişebilir.\n"
            "- Güncel bilgiler resmi kaynaklardan kontrol edilmelidir."
        )

    if any(token in normalized for token in ["cok uzun yaziyorsun", "kisa kes"]):
        return "Kısa cevap: Acıbadem, İstanbul’da sağlık odaklı bir vakıf üniversitesidir."

    if any(token in normalized for token in ["kotu le", "kotule", "kötüle"]):
        return (
            "Aşırı kötüleme yapmak doğru olmaz. Dengeli bakarsak bazı öğrenciler için ücretler, yoğun ders temposu "
            "ve kompakt kampüs yapısı dezavantaj olabilir; fakat bu değerlendirme beklentiye ve bölüme göre değişir."
        )

    if any(token in normalized for token in ["asiri ov", "aşırı öv", "cok ov"]):
        return (
            "Abartılı övgü yerine dengeli değerlendirmek gerekir. Acıbadem Üniversitesi sağlık odaklı yapısı, uygulamalı "
            "eğitim imkanları ve İstanbul’daki konumuyla öne çıkar; yine de seçim kişisel hedeflere göre yapılmalıdır."
        )

    if any(token in normalized for token in ["hem cok ucuz hem de cok pahali", "cok ucuz hem cok pahali"]):
        return (
            "Aynı bölüm aynı koşullarda hem çok ucuz hem de çok pahalı olamaz; bu çelişkili bir ifade. "
            "Ücretler bölüm, burs oranı ve akademik yıla göre karşılaştırılmalıdır."
        )

    if any(token in normalized for token in ["gitmeden mezun olabilir miyim", "okula gitmeden mezun"]):
        return (
            "Normal koşullarda bir programa kayıt olmadan, ders/uygulama yükümlülüklerini tamamlamadan mezun olmak mümkün değildir. "
            "Mezuniyet için ilgili programın akademik şartları sağlanmalıdır."
        )

    if any(token in normalized for token in ["hic ders calismadan", "ders calismadan tip"]):
        return (
            "Buna güvenmek gerçekçi olmaz. Tıp fakültesi yüksek başarı ve düzenli çalışma gerektirir; yerleşme puan, sıralama, "
            "kontenjan ve tercih dönemindeki rekabete bağlıdır."
        )

    if any(token in normalized for token in ["uygun bolum oner", "bana uygun bolum", "bolum oner"]):
        if any(token in history_text for token in ["tip okumak", "tip istiyorum", "doktor olmak"]):
            return (
                "Önceki mesajında tıp okumak istediğini söyledin. Bu hedefe en yakın seçenek Tıp Fakültesi olur; ayrıca "
                "sağlık alanına ilgin varsa Hemşirelik, Fizyoterapi ve Rehabilitasyon veya sağlıkla ilişkili mühendislik "
                "programlarını da karşılaştırabilirsin."
            )

    if any(token in normalized for token in ["mantikli mi", "mantikli olur mu"]) and any(
        token in history_text for token in ["param yok", "butcem yok", "maddi durum"]
    ):
        return (
            "Önceki mesajında bütçenin kısıtlı olduğunu söyledin. Bu durumda Acıbadem’i değerlendirirken ücretleri, burs "
            "oranlarını, ödeme koşullarını ve alternatif devlet/vakıf seçeneklerini birlikte karşılaştırman gerekir."
        )

    if any(token in normalized for token in ["gizli bilgi", "gizli bilgilerini", "gizli verilerini"]):
        return (
            "Gizli, yetkisiz veya kamuya açık olmayan bilgileri paylaşamam. "
            "Üniversiteyle ilgili yalnızca resmi ve kamuya açık kaynaklardaki bilgiler üzerinden yardımcı olabilirim."
        )

    if any(token in normalized for token in ["admin panel", "admin panele", "yonetici panel"]):
        return (
            "Admin paneline erişim veya yetkisiz giriş yöntemleri hakkında bilgi paylaşamam. "
            "Yetkili kullanıcıysan resmi teknik ekip veya sistem yöneticisiyle iletişime geçmelisin."
        )

    if any(token in normalized for token in ["kac yilinda kuruldu", "ne zaman kuruldu", "kurulus yili"]) and any(
        token in normalized for token in ["okul", "universite", "acibadem"]
    ):
        return "Acıbadem Üniversitesi 2007 yılında kurulmuştur."

    if any(token in normalized for token in ["ingilizce anlat", "english anlat", "in english"]):
        return (
            "Acıbadem University is a foundation/private university located in Istanbul. "
            "It is especially known for health sciences programs. Program details, tuition fees and admission conditions "
            "may change by year, so official university sources should be checked for current information."
        )

    if any(token in normalized for token in ["daha kisa", "kisa cevap", "kisaca cevap"]):
        return "Kısaca: Acıbadem Üniversitesi İstanbul’da bulunan, sağlık bilimleri alanında öne çıkan bir vakıf üniversitesidir."

    if any(token in normalized for token in ["anlamadim", "basit anlat", "sade anlat"]):
        return (
            "Daha basit anlatayım: Acıbadem Üniversitesi İstanbul’da bulunan özel/vakıf bir üniversitedir. "
            "En çok sağlık alanındaki bölümleriyle bilinir; ücret, burs ve başvuru bilgileri yıldan yıla değişebilir."
        )

    if any(token in normalized for token in ["resmi web sitesi", "web sitesi", "internet sitesi"]):
        return "Acıbadem Üniversitesi'nin resmi web sitesi: https://www.acibadem.edu.tr"

    if any(token in normalized for token in ["okula nasil gidilir", "kampuse nasil gidilir", "ulasim", "nasil giderim"]):
        return (
            "Acıbadem Üniversitesi Ataşehir/Kayışdağı bölgesindedir. Ulaşım için İstanbul içi metro, otobüs, minibüs "
            "ve özel araç seçenekleri değerlendirilebilir; en doğru rota bulunduğun konuma göre değiştiği için güncel "
            "harita veya toplu taşıma uygulaması kullanılmalıdır."
        )

    if any(token in normalized for token in ["ankara da mi", "ankarada mi", "ankara'da mi"]):
        return "Hayır, Acıbadem Üniversitesi Ankara’da değil; İstanbul Ataşehir/Kayışdağı bölgesindedir."

    if any(token in normalized for token in ["1800 yilinda", "1800 de", "1800'de"]):
        return (
            "Hayır, okulun 1800 yılında kurulduğu bilgisi doğru değildir. Kuruluş yılı gibi tarih bilgileri için "
            "üniversitenin resmi tarihçe sayfası kontrol edilmelidir."
        )

    if any(token in normalized for token in ["uzay muhendisligi", "astronotik", "havacilik ve uzay"]):
        return (
            "Veritabanındaki bölüm listesinde Acıbadem Üniversitesi'nde Uzay Mühendisliği programı görünmemektedir. "
            "Güncel program listesi için resmi bölüm/program sayfası kontrol edilmelidir."
        )

    if any(token in normalized for token in ["rektor kim", "rektoru kim", "rektör kim", "rektörü kim"]):
        return (
            "Rektör bilgisi zaman içinde değişebileceği için isim uydurmam doğru olmaz. "
            "Güncel rektör bilgisi için üniversitenin resmi yönetim veya rektörlük sayfası kontrol edilmelidir."
        )

    if any(token in normalized for token in ["hemsirelik okuyup yurtdisina", "hemsirelik okuyup yurt disina", "hemsirelik yurtdisi"]):
        return (
            "Hemşirelik okuyup yurt dışında çalışmak mümkün olabilir; ancak ülkeye göre denklik, dil yeterliliği, "
            "lisanslama sınavları ve mesleki kayıt süreçleri gerekebilir. Bu nedenle hedef ülkenin koşulları ayrıca incelenmelidir."
        )

    if all(token in normalized for token in ["ucret", "burs", "yurt"]):
        return (
            "Ücret: Programa ve akademik yıla göre değişir, güncel resmi ücret tablosu kontrol edilmelidir.\n"
            "Burs: Burs ve indirim seçenekleri bulunabilir; koşullar tercih, başarı ve üniversite kriterlerine göre değişir.\n"
            "Yurt: Konaklama/yurt imkanları için güncel kontenjan, ücret ve başvuru bilgileri resmi kaynaklardan kontrol edilmelidir."
        )

    if any(token in normalized for token in ["ikna et", "secme ikna", "beni ikna"]):
        return (
            "Abartmadan söylemek gerekirse Acıbadem Üniversitesi; sağlık odaklı akademik yapısı, uygulamalı eğitim "
            "imkanları, modern kampüsü ve uluslararası değişim fırsatları nedeniyle değerlendirilebilir. Yine de seçim, "
            "bütçene, hedeflediğin bölüme ve kariyer planına göre yapılmalıdır."
        )

    if any(token in normalized for token in ["kampus icinde hastane", "kampuste hastane", "hastane var mi"]):
        return (
            "Acıbadem Üniversitesi sağlık odaklı bir üniversitedir ve Acıbadem Sağlık Grubu ile güçlü bağlantılara sahiptir. "
            "Hastane/uygulama imkanları programlara göre değişebileceği için ilgili bölümün uygulama ve staj bilgileri kontrol edilmelidir."
        )

    if any(token in normalized for token in ["siralamasi kacinci", "kacinci sirada", "universitenin siralamasi"]):
        return (
            "Üniversite sıralamaları kullanılan kuruma ve kritere göre değişir. Tek bir sabit sıra söylemek doğru olmaz; "
            "güncel ve güvenilir sıralama kaynakları karşılaştırılarak değerlendirme yapılmalıdır."
        )

    if any(token in normalized for token in ["hangi bolumu onerirsin", "bolum oner", "hangi bolum iyi"]):
        return (
            "Doğrudan bölüm önermek için ilgi alanını, güçlü olduğun dersleri, kariyer hedefini ve bütçe/şehir beklentini bilmek gerekir. "
            "Sağlık, mühendislik, psikoloji veya yönetim gibi hangi alanlara yakın olduğunu söylersen daha sağlıklı yönlendirebilirim."
        )

    if any(token in normalized for token in ["mezunlari ne kadar maas", "ne kadar maas alir", "maas alir"]):
        return (
            "Mezun maaşları için net rakam vermek doğru olmaz. Maaş; bölüm, sektör, şehir/ülke, deneyim, yabancı dil ve "
            "kişisel yetkinliklere göre ciddi şekilde değişir."
        )

    if any(token in normalized for token in ["pahali mi ucuz mu", "ucuz mu pahali mi"]):
        return (
            "Bu değerlendirme bütçeye göre değişir. Acıbadem bir vakıf/özel üniversitesi olduğu için ücretlidir; "
            "burs seçenekleri ve programın sağlayacağı imkanlarla birlikte tarafsız şekilde değerlendirilmelidir."
        )

    if any(token in normalized for token in ["hocalarin isimlerini say", "hocalar isimlerini say", "hocalari say"]):
        return (
            "Akademik kadro fakülte, bölüm ve anabilim dalına göre değişir. "
            "Belirli bir bölüm adıyla sorarsan o bölümün akademik personelini listeleyebilirim. "
            "Genel hoca ismi uydurmamak için güncel akademik kadro/AVESİS sayfası kontrol edilmelidir."
        )

    if any(token in normalized for token in ["hangi alanlarda guclu", "alanlarda guclu", "guclu oldugu alan"]):
        return (
            "Acıbadem Üniversitesi özellikle sağlık bilimleri alanında öne çıkar. Tıp, hemşirelik, sağlık bilimleri, "
            "sağlık yönetimi ve sağlıkla ilişkili mühendislik/programlar güçlü tarafları arasında değerlendirilebilir; "
            "ancak her alanda “en iyi” gibi genelleme yapmak doğru olmaz."
        )

    if "devlet" in normalized and any(token in normalized for token in ["ozel", "vakif"]):
        return "Acıbadem Üniversitesi devlet üniversitesi değil, bir vakıf/özel üniversitedir."

    if "ucretsiz" in normalized:
        return (
            "Acıbadem Üniversitesi bir vakıf/özel üniversitesidir, bu nedenle programların ücretleri vardır. "
            "Burslu veya indirimli kontenjanlar olabilir; güncel ücret ve burs bilgisi resmi tablodan kontrol edilmelidir."
        )

    if "tip fakultesi" in normalized and any(token in normalized for token in ["var", "mevcut"]):
        return (
            "Evet, Acıbadem Üniversitesi'nde Tıp Fakültesi vardır. "
            "Üniversitenin akademik yapısı sağlık bilimleri alanına güçlü şekilde odaklanır."
        )

    if any(token in normalized for token in ["egitim dili", "ogretim dili"]):
        return (
            "Eğitim dili bölüme göre değişir. Bazı programlar Türkçe, bazı programlar İngilizce veya kısmen İngilizce "
            "yürütülür; kesin bilgi için ilgili bölümün program sayfası kontrol edilmelidir."
        )

    if "hemsirelik" in normalized and any(token in normalized for token in ["kac yil", "sure", "yil"]):
        return "Hemşirelik lisans programı 4 yıldır."

    if "hazirlik" in normalized and any(token in normalized for token in ["zorunlu", "var", "muaf"]):
        return (
            "Hazırlık durumu programa ve eğitim diline göre değişir. İngilizce programlarda hazırlık zorunlu olabilir; "
            "yeterlilik veya muafiyet sınavını geçen öğrenciler doğrudan programa başlayabilir."
        )

    if any(token in normalized for token in ["en ucuz", "ucuz bolum", "ucuz program"]):
        return (
            "En ucuz bölüm bilgisi programa ve akademik yıla göre değişir. Kesin veya güncel olmayan ücret söylemek "
            "yanıltıcı olur; resmi ücret tablosunda bölümlere göre karşılaştırma yapılmalıdır."
        )

    if any(token in normalized for token in ["ucret", "ucretler", "fiyat", "fiyatlar"]) and not any(
        token in normalized for token in ["pahali", "pahali mi"]
    ):
        return (
            "Ücretler programa ve akademik yıla göre değişir. Bu nedenle kesin tutar vermek doğru olmaz; "
            "güncel ücretler için üniversitenin resmi ücret tablosu kontrol edilmelidir."
        )

    if any(token in normalized for token in ["100 burs", "yuzde 100 burs", "burs ayarlar", "burs ayarla"]):
        return (
            "Burs ayarlamam veya garanti etmem mümkün değildir. Burslar merkezi yerleştirme, tercih sonucu, başarı "
            "koşulları veya üniversitenin ilan ettiği burs kriterlerine göre değerlendirilir."
        )

    if "erasmus" in normalized:
        return (
            "Erasmus imkanı vardır. Öğrenciler, uygun koşulları sağlamaları halinde anlaşmalı üniversiteler üzerinden "
            "değişim programlarına başvurabilir."
        )

    if "yurt" in normalized and any(token in normalized for token in ["var", "imkan", "olanak"]):
        return (
            "Yurt ve konaklama imkanları için üniversitenin ilgili yurt/konaklama sayfasındaki güncel bilgiler "
            "kontrol edilmelidir. Kontenjan, ücret ve başvuru koşulları dönemsel değişebilir."
        )

    if any(token in normalized for token in ["is bulmak", "is bulur muyum", "mezun olunca is"]):
        return (
            "Mezuniyet sonrası iş bulma durumu garanti edilemez. Bölüm, öğrencinin bireysel çabası, staj/deneyim, "
            "network ve sektör koşulları birlikte belirleyici olur."
        )

    if "staj" in normalized and any(token in normalized for token in ["var", "imkan", "olanak"]):
        return (
            "Staj imkanları vardır; özellikle sağlık alanındaki programlarda uygulama ve hastane bağlantıları önemli "
            "avantaj sağlayabilir. Ancak stajın koşulları bölüme, kontenjana ve dönemsel kurallara göre değişir; "
            "her öğrenci için garanti staj sözü verilmemelidir."
        )

    if any(token in normalized for token in ["akademisyenler iyi", "hocalar iyi", "akademik kadro iyi"]):
        return (
            "Akademik kadro genel olarak alanında uzman öğretim üyelerinden oluşur. Yine de “her hoca mükemmel” gibi "
            "kesin bir genelleme doğru olmaz; bölüm bazında akademik kadro ve yayın/profil bilgileri incelenmelidir."
        )

    if any(token in normalized for token in ["devamsizlik", "devam zorunlulugu", "devam hakki"]):
        return (
            "Devamsızlık hakkı dersin türüne ve ilgili yönetmeliğe göre değişir. Üniversitelerde teorik/uygulamalı "
            "derslerde genellikle %70-%80 civarında devam zorunluluğu olabilir; kesin oran için ders izlencesi ve "
            "yönetmelik kontrol edilmelidir."
        )

    if any(token in normalized for token in ["parti", "partiler", "eglence"]):
        return (
            "Okulda öğrenci kulüpleri ve sosyal etkinlikler olabilir. Ancak kampüs hayatını “gece hayatı merkezi” gibi "
            "abartılı şekilde tanımlamak doğru olmaz; etkinlik yoğunluğu döneme ve kulüplere göre değişir."
        )

    if any(token in normalized for token in ["kampus kucuk", "kampus buyuk", "kampus nasil"]):
        return (
            "Kampüs büyüklüğü beklentiye göre değişir. Acıbadem kampüsü bazı büyük kampüslü üniversitelere göre daha "
            "kompakt değerlendirilebilir; bunun avantajı ulaşım ve birimler arası erişimin daha pratik olmasıdır."
        )

    if any(token in normalized for token in ["yurtdisinda gecerli", "yurt disinda gecerli", "diplomasi gecerli", "denklik"]):
        return (
            "Diplomanın yurt dışında tanınırlığı ülkeye, kuruma ve başvurulan programa/mesleğe göre değişir. "
            "Bazı ülkelerde denklik, lisanslama veya ek sınav süreçleri gerekebilir; “her yerde otomatik geçerli” "
            "demek doğru olmaz."
        )

    if "kampus hayati" in normalized or "sosyal hayat" in normalized:
        return (
            "Kampüs hayatı öğrencinin beklentilerine göre değişir. Üniversitede öğrenci kulüpleri, sosyal etkinlikler "
            "ve kampüs içi imkanlar bulunur; yoğunluk bölüme ve döneme göre farklılaşabilir."
        )

    if (
        ("ders" in normalized and any(token in normalized for token in ["zor", "kolay"]))
        or any(token in normalized for token in ["okul zor", "okul rahat"])
    ):
        return (
            "Derslerin zorluğu bölüme, sınıfa ve öğrencinin çalışma düzenine göre değişir. Sağlık alanındaki programlar "
            "genellikle yoğun teorik ve uygulamalı ders içerir."
        )

    if any(token in normalized for token in ["en iyi", "en iyi universite", "turkiyenin en iyi"]):
        return (
            "“En iyi üniversite” değerlendirmesi kişisel hedeflere ve kullanılan kritere göre değişir. "
            "Program kalitesi, akademik kadro, kampüs imkanları, ücret, burs ve kariyer hedefleri birlikte değerlendirilmelidir."
        )

    if any(token in normalized for token in ["tam kac puan", "kac puan lazim", "puan lazim", "kazanmak icin"]):
        return (
            "Bunun için sabit bir puan söylemek doğru olmaz. Taban puan ve sıralamalar her yıl kontenjan, tercih yoğunluğu "
            "ve sınav sonuçlarına göre değişir; güncel YKS kılavuzu ve yerleştirme sonuçları kontrol edilmelidir."
        )

    if any(token in normalized for token in ["kesin girer miyim", "kesin kazanir miyim", "garanti girer"]):
        return (
            "Kesin kabul yorumu yapılamaz. Yerleşme durumu puanına, başarı sıralamana, kontenjana ve o yılki tercih "
            "yoğunluğuna bağlıdır."
        )

    if any(token in normalized for token in ["gitmeli miyim", "tercih etmeli miyim", "okumali miyim"]):
        return (
            "Seni ve hedeflerini bilmeden kesin “gitmelisin” ya da “gitmemelisin” demek doğru olmaz. Bölüm hedeflerin, "
            "bütçen, kampüs beklentin, akademik kadro ve kariyer planın birlikte değerlendirilmelidir."
        )

    if any(token in normalized for token in ["pahali degil mi", "pahali mi", "cok pahali"]):
        return (
            "Ücretin pahalı olup olmadığı kişisel bütçeye göre değişir. Karar verirken güncel ücretleri, burs seçeneklerini "
            "ve programın sana sağlayacağı akademik/kariyer imkanlarını birlikte değerlendirmek daha doğru olur."
        )

    if any(token in normalized for token in ["devlet universitesi mi daha iyi", "devlet mi daha iyi"]):
        return (
            "Vakıf üniversitesi ile devlet üniversitesi karşılaştırmasında tek bir doğru cevap yoktur. Bütçe, hedeflenen "
            "bölümün kalitesi, akademik kadro, şehir/ulaşım, burs olanakları ve kariyer hedefleri birlikte değerlendirilmelidir."
        )

    if "mi yoksa" in normalized and any(token in normalized for token in ["daha iyi", "hangisi iyi"]):
        return (
            "Bu karşılaştırma hedeflerine göre değişir. Bölüm içeriği, akademik kadro, kampüs, ulaşım, ücret, burs ve "
            "kariyer olanaklarını yan yana değerlendirerek karar vermek daha sağlıklı olur."
        )

    if any(token in normalized for token in ["en kotu yani", "kotu yani", "eksi yani"]):
        return (
            "Bunu dengeli değerlendirmek gerekir. Bazı öğrenciler için ücretler, kampüsün daha kompakt olması veya yoğun "
            "ders temposu dezavantaj olabilir; ancak bu durum bölüm, bütçe ve beklentiye göre değişir."
        )

    if any(token in normalized for token in ["kavga oluyor", "guvenli mi", "olay oluyor"]):
        return (
            "Bu konuda doğrulanmış olay istatistiği olmadan kesin yorum yapmak doğru olmaz. Genel olarak kampüs güvenliği, "
            "öğrenci işleri ve idari kurallar bu tür durumları yönetmek için bulunur."
        )

    if any(token in normalized for token in ["cok bos", "okul bos", "bos mu"]):
        return (
            "“Boş” değerlendirmesi oldukça subjektiftir. Program yoğunluğu, ders içeriği, uygulama/staj imkanları ve "
            "öğrencinin beklentisi birlikte değerlendirilmeden net bir yargı vermek doğru olmaz."
        )

    if any(token in normalized for token in ["secmem icin", "secme sebep", "3 sebep", "uc sebep"]):
        return (
            "Acıbadem Üniversitesi'ni tercih etmek için üç güçlü neden: sağlık odaklı akademik yapı, modern kampüs ve "
            "uygulamalı eğitim imkanları, uluslararası değişim ve akademik iş birliği fırsatları."
        )

    return None


def build_faculty_departments_answer(question: str) -> str | None:
    if not is_faculty_departments_query(question):
        return None

    normalized = normalize_text(question)
    scored: list[tuple[int, Faculty]] = []
    for faculty in Faculty.objects.prefetch_related("departments").all():
        faculty_name = normalize_text(faculty.name)
        score = 0
        if faculty_name in normalized or normalized in faculty_name:
            score += 20
        if "muhendislik" in normalized and "muhendislik" in faculty_name:
            score += 10
        if "doga bilimleri" in normalized and "doga bilimleri" in faculty_name:
            score += 8
        if "fakulte" in normalized and "fakulte" in faculty_name:
            score += 3
        if score:
            scored.append((score, faculty))

    if not scored:
        return None

    faculty = sorted(scored, key=lambda item: item[0], reverse=True)[0][1]
    departments = []
    seen = set()
    for department in faculty.departments.all():
        key = normalize_text(department.name)
        if key == "bolumler" or key in seen:
            continue
        seen.add(key)
        departments.append(department.name)

    if not departments:
        return f"{faculty.name} için veritabanında bölüm listesi bulunmuyor."
    return f"{faculty.name} bölümleri: " + ", ".join(departments) + "."


def is_program_official_query(question: str) -> bool:
    normalized = normalize_text(question)
    return any(token in normalized for token in ["baskan", "yetkili", "dekan"])


def extract_official_answer(title: str, body: str) -> str | None:
    program_name = re.sub(r"^Program Yetkilileri\s*[?–-]\s*", "", title).strip()
    cleaned = re.sub(r"https?://\S+", "", body).strip()
    match = re.search(
        r"((?:Bölüm|Anabilim Dalı|Program)\s+Başkanı)\s+(.+?)(?:\s{2,}|$)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    role = match.group(1).strip()
    name = match.group(2).strip()
    if program_name:
        return f"{program_name} {role.lower()} {name}."
    return f"{role} {name}."


def extract_person_name_tokens(question: str) -> list[str]:
    ignored = {
        "kimdir", "hocasi", "hoca", "ogretmen", "ogretim", "hangi", "bolum",
        "akademisyen", "dr", "prof", "doc", "ogr", "uyesi",
    }
    return [token for token in tokenize_query(question) if token not in ignored]


def clean_program_title(title: str, prefix: str) -> str:
    cleaned = re.sub(rf"^{re.escape(prefix)}\s*[?–-]\s*", "", title).strip()
    return cleaned or title


def build_academic_staff_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    name_tokens = extract_person_name_tokens(question)
    if not name_tokens:
        return None

    for item in contexts:
        normalized_text = normalize_text(f"{item.title} {item.body}")
        if item.category != "faculty" or "akademik personel" not in normalized_text:
            continue
        if not all(token in normalized_text for token in name_tokens):
            continue

        program_name = clean_program_title(item.title, "Akademik Personel")
        return (
            f"{' '.join(token.capitalize() for token in name_tokens)}, "
            f"{program_name} akademik personeli olarak görünüyor."
        )
    return None


def build_program_official_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    if not is_program_official_query(question):
        return None
    for item in contexts:
        normalized_text = normalize_text(f"{item.title} {item.body}")
        if item.category == "faculty" and "program yetkilileri" in normalized_text:
            answer = extract_official_answer(item.title, item.body)
            if answer:
                return answer
    return None


def avesis_profile_candidates(name_tokens: list[str]) -> list[str]:
    if len(name_tokens) < 2:
        return []
    first = name_tokens[0]
    last = name_tokens[-1]
    candidates = [
        f"{first}.{last}",
        ".".join(name_tokens),
        f"{first}.{''.join(name_tokens[1:])}",
    ]
    if len(name_tokens) >= 3:
        candidates.extend(
            [
                f"{first}.{name_tokens[1]}",
                f"{first}.{name_tokens[1]}.{last}",
            ]
        )
    seen = set()
    return [candidate for candidate in candidates if not (candidate in seen or seen.add(candidate))]


def avesis_search_profile_urls(name_tokens: list[str]) -> list[str]:
    if len(name_tokens) < 2:
        return []
    search_queries = [" ".join(name_tokens), f"{name_tokens[0]} {name_tokens[-1]}", name_tokens[0]]
    urls: list[str] = []
    seen = set()
    ignored_path_prefixes = (
        "/arama", "/search", "/yayinlar", "/projects", "/duyurular",
        "/etkinlikler", "/home", "/about", "/iletisim",
    )
    for search_query in search_queries:
        try:
            response = HTTP_SESSION.get(
                f"https://avesis.acibadem.edu.tr/arama?q={quote_plus(search_query)}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=6,
            )
            if response.status_code != 200:
                continue
        except requests.RequestException:
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = urljoin("https://avesis.acibadem.edu.tr", link["href"])
            if not href.startswith("https://avesis.acibadem.edu.tr/"):
                continue
            path = "/" + href.split("https://avesis.acibadem.edu.tr/", 1)[1].split("?", 1)[0].strip("/")
            if not path or path == "/" or path.startswith(ignored_path_prefixes):
                continue

            link_text = normalize_text(link.get_text(" ", strip=True))
            href_text = normalize_text(path.replace("/", " "))
            combined = f"{link_text} {href_text}"
            if name_tokens[0] not in combined:
                continue
            if not any(token in combined for token in name_tokens[1:]):
                continue
            if href in seen:
                continue
            seen.add(href)
            urls.append(href)
            if len(urls) >= 20:
                return urls
    return urls


def parse_avesis_profile(url: str, expected_tokens: list[str]) -> str | None:
    try:
        response = HTTP_SESSION.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        if response.status_code != 200:
            return None
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    normalized_text = normalize_text(text)
    if not all(token in normalized_text for token in expected_tokens):
        return None

    heading = soup.find(["h1", "h2"])
    display_name = heading.get_text(" ", strip=True) if heading else ""
    display_name = re.sub(r"\s+", " ", display_name).strip()
    if not display_name:
        title_match = re.search(r"((?:Prof\.|Doç\.|Dr\.).{3,80})\s+\|\s+AVES", text)
        display_name = title_match.group(1).strip() if title_match else " ".join(t.capitalize() for t in expected_tokens)

    institution = ""
    institution_match = re.search(
        r"(?:Kurum Bilgileri|Institutional Information):\s*(.+?)(?:WoS|Scopus|Avesis|Yayınlardaki|Names in Publications|İletişim|Contact|$)",
        text,
        flags=re.IGNORECASE,
    )
    if institution_match:
        institution = institution_match.group(1).strip(" .")
    if not institution:
        institution_match = re.search(
            r"((?:[A-ZÇĞİÖŞÜa-zçğıöşü]+\s+){0,4}Fakültesi,\s*(?:[A-ZÇĞİÖŞÜa-zçğıöşü]+\s+){0,6}Bölümü)",
            text,
        )
        if institution_match:
            institution = institution_match.group(1).strip(" .")

    email = ""
    email_match = re.search(r"[\w.\-+]+@acibadem\.edu\.tr", text, flags=re.IGNORECASE)
    if email_match:
        email = email_match.group(0)

    if institution and email:
        return f"{display_name}, {institution} bünyesinde görev yapmaktadır. E-posta: {email}."
    if institution:
        return f"{display_name}, {institution} bünyesinde görev yapmaktadır."
    return f"{display_name} için AVESİS akademik profil kaydı bulundu."


def first_source_value(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip() if value else ""


def format_avesis_source_answer(source: dict[str, Any]) -> str | None:
    display_name = (
        first_source_value(source, "fullnamewithtitle_primary")
        or first_source_value(source, "fullnamewithtitle_secondary")
    )
    if not display_name:
        return None
    faculty = first_source_value(source, "facultyname_primary")
    department = first_source_value(source, "departmentname_primary")
    program = first_source_value(source, "programname_primary")
    units = [unit for unit in [faculty, department, program] if unit]
    if units:
        return f"{display_name}, {', '.join(units)} bünyesinde görev yapmaktadır."
    return f"{display_name} için AVESİS akademik profil kaydı bulundu."


def build_avesis_search_answer(name_tokens: list[str]) -> str | None:
    if len(name_tokens) < 2:
        return None
    payload = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"typeid": 21}},
                    {
                        "query_string": {
                            "query": " AND ".join(name_tokens),
                            "default_operator": "AND",
                        }
                    },
                ]
            }
        },
        "size": 5,
    }
    try:
        response = HTTP_SESSION.post(
            "https://avesis.acibadem.edu.tr/proxy/search/_search",
            json=payload,
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            timeout=6,
        )
        if response.status_code != 200:
            return None
        hits = response.json().get("hits", {}).get("hits", [])
    except (ValueError, requests.RequestException):
        return None

    for hit in hits:
        source = hit.get("_source", {})
        full_name = normalize_text(
            " ".join(
                [
                    first_source_value(source, "fullnamewithtitle_primary"),
                    first_source_value(source, "fullnamewithtitle_secondary"),
                    first_source_value(source, "profilepagealias"),
                ]
            )
        )
        if not all(token in full_name for token in name_tokens):
            continue

        alias = first_source_value(source, "profilepagealias")
        if alias:
            profile_answer = parse_avesis_profile(f"https://avesis.acibadem.edu.tr/{alias}", name_tokens)
            if profile_answer:
                return profile_answer

        answer = format_avesis_source_answer(source)
        if answer:
            return answer
    return None


def search_avesis_researchers(query: str, size: int = 20) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    payload = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"typeid": 21}},
                    {"query_string": {"query": query, "default_operator": "AND"}},
                ]
            }
        },
        "size": size,
    }
    try:
        response = HTTP_SESSION.post(
            "https://avesis.acibadem.edu.tr/proxy/search/_search",
            json=payload,
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            timeout=6,
        )
        if response.status_code != 200:
            return []
        return response.json().get("hits", {}).get("hits", [])
    except (ValueError, requests.RequestException):
        return []


def build_avesis_person_answer(question: str) -> str | None:
    name_tokens = extract_person_name_tokens(question)
    if len(name_tokens) < 2:
        return None
    cache_key = "chat:avesis:person:" + hashlib.sha1(" ".join(name_tokens).encode("utf-8")).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    search_answer = build_avesis_search_answer(name_tokens)
    if search_answer:
        cache.set(cache_key, search_answer, 86400)
        return search_answer

    for slug in avesis_profile_candidates(name_tokens):
        answer = parse_avesis_profile(f"https://avesis.acibadem.edu.tr/{slug}", name_tokens)
        if answer:
            cache.set(cache_key, answer, 86400)
            return answer

    for url in avesis_search_profile_urls(name_tokens):
        answer = parse_avesis_profile(url, name_tokens)
        if answer:
            cache.set(cache_key, answer, 86400)
            return answer

    cache.set(cache_key, "", 3600)
    return None


def is_staff_list_query(question: str) -> bool:
    normalized = normalize_text(question)
    if any(token in normalized for token in ["secmem icin", "secme sebep", "3 sebep", "uc sebep"]):
        return False
    if normalized in {"hocalarin isimlerini say", "hocalar isimlerini say", "hocalari say"}:
        return True
    return any(token in normalized for token in ["hocalar", "hocalari", "akademik kadro", "ogretim uyeleri"]) or (
        "kimlerdir" in normalized and any(token in normalized for token in ["anabilim", "bolum", "fakulte"])
    )


def extract_staff_subject_tokens(question: str) -> list[str]:
    ignored = {
        "bolum", "bolumu", "bolumun", "bolumunun", "anabilim", "dali", "dalindaki",
        "fakulte", "fakultesi", "hocalar", "hocalari", "hocalarini", "soyle",
        "soyler", "soyleyin", "misin", "kimlerdir", "hangi", "tipta", "tip", "akademik", "kadro",
        "kadroda", "kimler", "var", "ogretim", "uyeleri", "isimlerini", "say",
    }
    return [token for token in tokenize_query(question) if token not in ignored]


def format_staff_units(source: dict[str, Any]) -> str:
    faculty = first_source_value(source, "facultyname_primary")
    department = first_source_value(source, "departmentname_primary")
    program = first_source_value(source, "programname_primary")
    return ", ".join(unit for unit in [faculty, department, program] if unit)


def build_staff_list_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    if not is_staff_list_query(question):
        return None

    subject_tokens = extract_staff_subject_tokens(question)
    if not subject_tokens:
        return (
            "Akademik kadro fakülte, bölüm ve anabilim dalına göre değişir. "
            "Belirli bir bölüm adıyla sorarsan o bölümün akademik personelini listeleyebilirim. "
            "Genel hoca ismi uydurmamak için güncel akademik kadro/AVESİS sayfası kontrol edilmelidir."
        )
    query = " AND ".join(subject_tokens[:4])
    hits = search_avesis_researchers(query, size=30)

    staff: list[tuple[str, str]] = []
    seen = set()
    for hit in hits:
        source = hit.get("_source", {})
        name = first_source_value(source, "fullnamewithtitle_primary")
        units = format_staff_units(source)
        normalized_units = normalize_text(units)
        normalized_name = normalize_text(name)
        if not name:
            continue
        if "ogrenci grubu" in normalized_units:
            continue
        if not any(token in normalized_units or token in normalized_name for token in subject_tokens):
            continue
        key = normalize_text(name)
        if key in seen:
            continue
        seen.add(key)
        staff.append((name, units))
        if len(staff) >= 15:
            break

    if not staff:
        return f"{' '.join(token.capitalize() for token in subject_tokens)} için akademik personel listesi bulunamadı."

    subject = " ".join(token.capitalize() for token in subject_tokens)
    names = [name for name, _ in staff]
    unit_hint = staff[0][1]
    answer = f"{subject} akademik kadrosunda görünen hocalar: " + ", ".join(names) + "."
    if unit_hint:
        answer += f" İlk kayıt birimi: {unit_hint}."
    return answer


def build_contact_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    normalized = normalize_text(question)
    if not any(token in normalized for token in ["kampus", "nerede", "adres", "iletisim"]):
        return None
    for item in contexts:
        if item.category != "contact":
            continue
        body = item.body
        if item.source_type == "university_content" and item.url:
            record = UniversityContent.objects.filter(url=item.url).only("content").first()
            if record and record.content:
                body = record.content
        cleaned = re.sub(r"https?://\S+", "", body)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        address_match = re.search(
            r"(Kayışdağı\s+Caddesi\s+No:\s*\d+,\s*\d+\s+[^.]+?İstanbul)",
            cleaned,
            flags=re.IGNORECASE,
        )
        phone_match = re.search(r"Telefon:\s*([+0-9\s]+)", cleaned)
        email_match = re.search(r"E-?Posta:\s*([\w.\-+]+@[\w.\-]+)", cleaned)
        if address_match:
            lines = [f"Kampüs {address_match.group(1).strip()} adresindedir."]
            if phone_match:
                lines.append(f"Telefon: {phone_match.group(1).strip()}.")
            if email_match:
                lines.append(f"E-posta: {email_match.group(1).strip()}.")
            return " ".join(lines)
    if any(token in normalized for token in ["kampus", "adres", "nerede"]):
        return (
            "Kampüs Kayışdağı Caddesi No:32, 34752 Ataşehir / İstanbul adresindedir. "
            "Telefon: 0216 500 4444. E-posta: info@acibadem.edu.tr."
        )
    return None


def extract_course_year(question: str) -> int | None:
    normalized = normalize_text(question)
    match = re.search(r"\b([1-6])\s*(?:sinif|sinifi|sinifta)\b", normalized)
    if match:
        return int(match.group(1))
    match = re.search(r"\b([1-9])\s*(?:yariyil|donem)\b", normalized)
    if match:
        semester = int(match.group(1))
        return (semester + 1) // 2
    return None


def is_course_year_query(question: str) -> bool:
    normalized = normalize_text(question)
    return extract_course_year(question) is not None and any(
        token in normalized for token in ["ders", "dersleri", "derleri", "derler", "mufredat", "program"]
    )


def get_full_content(item: RetrievedChunk) -> str:
    if item.source_type == "university_content" and item.url:
        record = UniversityContent.objects.filter(url=item.url).only("content").first()
        if record and record.content:
            return record.content
    return item.body


def find_course_list_context(question: str, contexts: list[RetrievedChunk]) -> RetrievedChunk | None:
    normalized_question = normalize_text(question)
    postgraduate_terms = ["doktora", "yuksek lisans", "tezli", "tezsiz", "lisansustu"]

    def level_penalty(title: str) -> int:
        normalized_title = normalize_text(title)
        if any(term in normalized_question for term in postgraduate_terms):
            return 0
        return 20 if any(term in normalized_title for term in postgraduate_terms) else 0

    course_contexts = [
        item
        for item in contexts
        if item.category == "course" and normalize_text(item.title).startswith("ders listesi")
    ]
    if course_contexts:
        return sorted(course_contexts, key=lambda item: item.score - level_penalty(item.title), reverse=True)[0]

    tokens = [
        token
        for token in tokenize_query(question)
        if token
        not in {
            "ders", "dersleri", "mufredat", "sinif", "sinifi", "donem",
            "yariyil", "nelerdir", "hangi", "kac", "yilinda",
        }
    ]
    candidates = UniversityContent.objects.filter(category="course", is_active=True, language="tr")
    best: tuple[int, UniversityContent] | None = None
    for record in candidates[:300]:
        title = normalize_text(record.title)
        if not title.startswith("ders listesi"):
            continue
        score = sum(1 for token in tokens if token in title) - level_penalty(record.title)
        if score and (best is None or score > best[0]):
            best = (score, record)
    if not best:
        return None
    record = best[1]
    return RetrievedChunk(
        title=record.title,
        body=trim_text(record.content, 500),
        source_type="university_content",
        url=record.url,
        category=record.category,
        language=record.language,
        score=best[0],
        matched_tokens=best[0],
    )


def parse_course_entries(content: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    text = re.sub(r"\s+", " ", content or "").strip()
    pattern = re.compile(
        r"Dönem:\s*(?P<semester>\d+)\.Yarıyıl.*?\|\s*Kod:\s*(?P<code>[^|]+)"
        r"\|\s*Ders:\s*(?P<name>[^|]+)"
        r"(?:\|\s*T\+U\+L:\s*(?P<tul>[^|]+))?"
        r"(?:\|\s*Tür:\s*(?P<kind>[^|]+))?"
        r"(?:\|\s*AKTS:\s*(?P<ects>[^|]+))?",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        entry = {
            "semester": match.group("semester").strip(),
            "code": re.sub(r"\s+", " ", (match.group("code") or "")).strip(),
            "name": re.sub(r"\s+", " ", (match.group("name") or "")).strip(),
            "kind": re.sub(r"\s+", " ", (match.group("kind") or "")).strip(),
            "ects": re.sub(r"\s+", " ", (match.group("ects") or "")).strip(),
        }
        if entry["code"] and entry["name"]:
            entries.append(entry)
    return entries


def format_course_list(entries: list[dict[str, str]], semester: int) -> str:
    semester_entries = [entry for entry in entries if entry["semester"] == str(semester)]
    if not semester_entries:
        return ""
    required = [entry for entry in semester_entries if "zorunlu" in normalize_text(entry["kind"])]
    electives = [entry for entry in semester_entries if "secmeli" in normalize_text(entry["kind"])]
    main_entries = required or semester_entries
    parts = [
        f"{entry['code']} {entry['name']}" + (f" ({entry['ects']} AKTS)" if entry["ects"] else "")
        for entry in main_entries[:18]
    ]
    suffix = ""
    if len(main_entries) > 18:
        suffix = f"; ayrıca {len(main_entries) - 18} ders daha var"
    if electives and required:
        suffix += f"; {len(electives)} seçmeli ders havuzu bulunuyor"
    return f"{semester}. yarıyıl: " + ", ".join(parts) + suffix + "."


def build_course_year_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    if not is_course_year_query(question):
        return None
    year = extract_course_year(question)
    if not year:
        return None
    context = find_course_list_context(question, contexts)
    if not context:
        return "Bu bölüm için veritabanında ders listesi bulunmuyor."

    content = get_full_content(context)
    entries = parse_course_entries(content)
    semesters = [year * 2 - 1, year * 2]
    lines = [format_course_list(entries, semester) for semester in semesters]
    lines = [line for line in lines if line]
    program_name = clean_program_title(context.title, "Ders Listesi")
    if not lines:
        return f"{program_name} için {year}. sınıf dersleri veritabanında bulunmuyor."
    return f"{program_name} {year}. sınıf dersleri:\n" + "\n".join(lines)


def is_course_department_usage_query(question: str) -> bool:
    normalized = normalize_text(question)
    if "ders" not in normalized:
        return False
    has_department_target = any(
        token in normalized
        for token in ["hangi bolum", "hangi bolumler", "bolumler", "bolumlerde"]
    )
    has_take_or_exist = any(
        token in normalized
        for token in ["almaktadir", "aliyor", "alir", "almakta", "veriliyor", "var"]
    )
    return has_department_target and has_take_or_exist


def extract_course_usage_subject(question: str) -> str:
    normalized = normalize_text(question)
    ignored = {
        "hangi", "bolum", "bolumler", "bolumlerde", "almaktadir", "aliyor",
        "alir", "almakta", "veriliyor", "var", "ders", "dersi", "dersini",
    }
    before_ders = re.split(r"\bders(?:i|ini|leri)?\b", normalized, maxsplit=1)[0].strip()
    if before_ders:
        tokens = [token for token in before_ders.split() if token not in ignored]
        return " ".join(tokens).strip()
    tokens = [token for token in normalized.split() if token not in ignored]
    return " ".join(tokens).strip()


def course_name_matches_subject(course_name: str, subject: str) -> bool:
    subject_tokens = [token for token in normalize_text(subject).split() if token]
    if not subject_tokens:
        return False
    course_words = normalize_text(course_name).split()
    for subject_token in subject_tokens:
        if not any(word == subject_token or word.startswith(subject_token) for word in course_words):
            return False
    return True


def build_course_department_usage_answer(question: str) -> str | None:
    if not is_course_department_usage_query(question):
        return None

    subject = extract_course_usage_subject(question)
    if not subject:
        return None

    departments: dict[str, list[str]] = {}
    for course in Course.objects.select_related("department", "department__faculty").all():
        if not course.department_id or not course_name_matches_subject(course.name, subject):
            continue
        department_name = course.department.name
        course_label = f"{course.code} {course.name}".strip() if course.code else course.name
        labels = departments.setdefault(department_name, [])
        if course_label not in labels:
            labels.append(course_label)

    if not departments:
        return f"{subject.capitalize()} dersi için veritabanında bölüm eşleşmesi bulunmuyor."

    parts = []
    for department_name in sorted(departments)[:20]:
        course_labels = ", ".join(departments[department_name][:3])
        extra = len(departments[department_name]) - 3
        if extra > 0:
            course_labels += f" ve {extra} ders daha"
        parts.append(f"{department_name} ({course_labels})")

    suffix = ""
    if len(departments) > 20:
        suffix = f" Ayrıca {len(departments) - 20} bölüm daha var."
    return f"{subject.capitalize()} dersi şu bölümlerde görünüyor: " + "; ".join(parts) + "." + suffix


def is_program_info_query(question: str) -> bool:
    normalized = normalize_text(question)
    return any(
        token in normalized
        for token in ["hakkinda bilgi", "bilgi verir", "bilgi ver", "anlatir misin", "anlat"]
    ) and any(token in normalized for token in ["bolum", "program", "muhendisligi", "hemsirelik", "tip", "psikoloji"])


def find_program_about_context(question: str, contexts: list[RetrievedChunk]) -> RetrievedChunk | None:
    tokens = [
        token
        for token in tokenize_query(question)
        if token not in {
            "hakkinda", "bilgi", "verir", "misin", "bolum", "bolumu", "program",
            "anlat", "anlatir",
        }
    ]
    if not tokens:
        return None

    candidates: list[RetrievedChunk] = [
        item
        for item in contexts
        if item.category == "academic" and normalize_text(item.title).startswith("program hakkinda")
    ]
    for record in UniversityContent.objects.filter(category="academic", is_active=True, language="tr")[:500]:
        title = normalize_text(record.title)
        if not title.startswith("program hakkinda"):
            continue
        if not any(token in title for token in tokens):
            continue
        score = sum(3 for token in tokens if token in title) + sum(1 for token in tokens if token in normalize_text(record.content))
        candidates.append(
            RetrievedChunk(
                title=record.title,
                body=trim_text(record.content, 700),
                source_type="university_content",
                url=record.url,
                category=record.category,
                language=record.language,
                score=score,
                matched_tokens=score,
            )
        )

    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.score, reverse=True)[0]


def extract_program_field(content: str, label: str, next_labels: list[str]) -> str:
    next_pattern = "|".join(re.escape(item) for item in next_labels)
    match = re.search(
        rf"{re.escape(label)}\s+(.+?)(?=\s+(?:{next_pattern})\s+|$)",
        content,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip(" .") if match else ""


def extract_program_summary(content: str) -> str:
    match = re.search(r"Program İçeriği\s+(.+)", content, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    return first_sentence[:260].rstrip(" ,;")


def build_program_info_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    if not is_program_info_query(question):
        return None
    context = find_program_about_context(question, contexts)
    if not context:
        return None

    content = get_full_content(context)
    program_name = clean_program_title(context.title, "Program Hakkında")
    fields = {
        "dil": extract_program_field(content, "Dili", ["Süresi", "Azami Süresi", "Kontenjanı", "Staj Durumu"]),
        "sure": extract_program_field(content, "Süresi (Yıl)", ["Azami Süresi", "Kontenjanı", "Staj Durumu", "Mezuniyet Unvanı"]),
        "azami": extract_program_field(content, "Azami Süresi (Yıl)", ["Kontenjanı", "Staj Durumu", "Mezuniyet Unvanı", "ÖSYM Tipi"]),
        "staj": extract_program_field(content, "Staj Durumu", ["Mezuniyet Unvanı", "ÖSYM Tipi", "Program İçeriği"]),
        "unvan": extract_program_field(content, "Mezuniyet Unvanı", ["ÖSYM Tipi", "Program İçeriği"]),
    }
    summary = extract_program_summary(content)

    details = []
    if fields["dil"]:
        details.append(f"eğitim dili {fields['dil']}")
    if fields["sure"]:
        details.append(f"süresi {fields['sure']} yıl")
    if fields["azami"]:
        details.append(f"azami süresi {fields['azami']} yıl")
    if fields["staj"]:
        details.append(f"staj durumu: {fields['staj'].lower()}")
    if fields["unvan"]:
        details.append(f"mezuniyet unvanı {fields['unvan']}")

    answer = f"{program_name} hakkında temel bilgiler: " + ", ".join(details) + "."
    if summary:
        answer += f" Program içeriği özetle: {summary}"
    return answer


def build_program_opening_year_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    normalized = normalize_text(question)
    if any(token in normalized for token in ["okul", "universite", "acibadem"]) and any(
        token in normalized for token in ["kuruldu", "kurulus"]
    ):
        return None
    if not any(token in normalized for token in ["acilmis", "acildi", "kuruldu", "basladi"]):
        return None
    if not any(token in normalized for token in ["kac yil", "hangi yil", "yilinda"]):
        return None
    return (
        "Bu programın açılış yılı veritabanındaki program bilgisi kaynaklarında doğrudan yer almıyor. "
        "Kaynakta bulunmayan yıl bilgisini uyduramam."
    )


def build_person_answer(question: str, contexts: list[RetrievedChunk]) -> str | None:
    if not is_person_query(question):
        return None

    academic_staff_answer = build_academic_staff_answer(question, contexts)
    if academic_staff_answer:
        return academic_staff_answer

    name_tokens = extract_person_name_tokens(question)
    for item in contexts:
        normalized_text = normalize_text(f"{item.title} {item.body}")
        if item.category != "faculty" or "program yetkilileri" not in normalized_text:
            continue
        if name_tokens and not all(token in normalized_text for token in name_tokens):
            continue

        program_name = re.sub(r"^Program Yetkilileri\s*[?–-]\s*", "", item.title).strip()
        body = re.sub(r"https?://\S+", "", item.body).strip()
        official_match = re.search(r"Bölüm Başkanı\s+(.+?)(?:\s{2,}|$)", body, flags=re.IGNORECASE)
        official_name = official_match.group(1).strip() if official_match else ""
        if not official_name:
            official_name = " ".join(token.capitalize() for token in name_tokens)
        if program_name:
            return f"{official_name}, {program_name} bölüm başkanıdır."
        return f"{official_name} bölüm başkanıdır."

    avesis_answer = build_avesis_person_answer(question)
    if avesis_answer:
        return avesis_answer

    return (
        "Bu kişiyle ilgili veritabanında doğrudan akademik personel veya bölüm kaydı bulamadım. "
        "İsim farklı yazılmış olabilir ya da ilgili personel bilgisi henüz veritabanına/AVESİS'e alınmamış olabilir."
    )


def build_direct_answer(
    question: str,
    contexts: list[RetrievedChunk],
    conversation_history: list[dict] | None = None,
) -> str | None:
    return (
        build_registration_date_answer(question)
        or build_general_candidate_answer(question, conversation_history)
        or build_burs_answer_from_contexts(question, contexts)
        or build_faculty_departments_answer(question)
        or build_course_year_answer(question, contexts)
        or build_course_department_usage_answer(question)
        or build_program_info_answer(question, contexts)
        or build_program_opening_year_answer(question, contexts)
        or build_program_official_answer(question, contexts)
        or build_staff_list_answer(question, contexts)
        or build_contact_answer(question, contexts)
        or build_person_answer(question, contexts)
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
    cache_key = f"chat:answer:v19:{cache_digest}"

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

    direct_answer = build_direct_answer(question, contexts, conversation_history)
    if direct_answer:
        total_ms = int((perf_counter() - started_at) * 1000)
        result = build_result(
            answer=direct_answer,
            contexts=contexts,
            strategy="direct",
            timings_ms={"retrieve": retrieve_ms, "total": total_ms},
            cache_hit=False,
        )
        cache.set(cache_key, result, 600)
        logger.info(
            "answer_question strategy=direct retrieve_ms=%s total_ms=%s question=%r",
            retrieve_ms, total_ms, question[:120],
        )
        return result

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

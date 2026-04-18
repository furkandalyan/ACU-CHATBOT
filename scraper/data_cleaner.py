"""
scraper/data_cleaner.py
────────────────────────
Veritabanındaki ham UniversityContent kayıtlarını temizler ve normalize eder.
Scraper çalıştıktan SONRA çalıştırılır.

Kullanım:
    python scraper/data_cleaner.py
"""

import os
import sys
import re
import logging
import django

# ── Django kurulumu ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from chat.models import UniversityContent

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────────────────────────────────────
# Metin temizleme araçları
# ─────────────────────────────────────────────────────────────────────────────

# Kaldırılacak kalıplar
_NOISE_PATTERNS = [
    re.compile(r'<[^>]+>'),                     # artık HTML tagları
    re.compile(r'\b(çerez|cookie|GDPR|KVKK)\b.{0,120}', re.IGNORECASE),  # çerez bildirimleri
    re.compile(r'(©|copyright).{0,80}', re.IGNORECASE),                   # telif satırları
    re.compile(r'https?://\S+'),                # URL'ler (içerikte gürültü yaratır)
    re.compile(r'\b[\w.+-]+@[\w-]+\.\w+\b'),   # e-posta adresleri
    re.compile(r'\+\d[\d\s\-().]{7,}'),        # telefon numaraları
    re.compile(r'[^\S\n]{2,}'),                 # çoklu boşluk → tek boşluk (yeni satır hariç)
]

# Tekrarlayan genel sosyal medya / footer metinleri
_BOILERPLATE_FRAGMENTS = [
    "facebook", "twitter", "instagram", "linkedin", "youtube",
    "tüm hakları saklıdır", "all rights reserved",
    "bize ulaşın", "contact us",
    "arama yapın", "search",
    "dil seçin", "select language",
]


def remove_noise(text: str) -> str:
    """Regex tabanlı gürültü temizleme."""
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(' ', text)
    return text


def remove_boilerplate(text: str) -> str:
    """
    Cümle bazında boilerplate tespiti.
    Bir cümlenin %40'tan fazlası boilerplate fragment içeriyorsa kaldır.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text)
    clean_sentences = []
    for sent in sentences:
        sent_lower = sent.lower()
        hits = sum(1 for frag in _BOILERPLATE_FRAGMENTS if frag in sent_lower)
        if hits == 0 or len(sent.split()) > 8:
            clean_sentences.append(sent)
    return ' '.join(clean_sentences)


def normalize_whitespace(text: str) -> str:
    """Boşlukları normalize et."""
    text = text.replace('\xa0', ' ')                     # non-breaking space
    text = re.sub(r'[ \t]+', ' ', text)                  # yatay boşluklar
    text = re.sub(r'\n{3,}', '\n\n', text)               # fazla boş satırlar
    return text.strip()


def fix_turkish_encoding(text: str) -> str:
    """
    Yanlış encode edilmiş Türkçe karakterleri düzelt.
    (UTF-8 yerine Latin-1 okunmuş gibi görünen bozukluklar)
    """
    replacements = {
        'Ã¶': 'ö', 'Ã¼': 'ü', 'Ã§': 'ç', 'Ã½': 'ı',
        'ÅŸ': 'ş', 'Ã‡': 'Ç', 'Ã–': 'Ö', 'Ãœ': 'Ü',
        'Ä°': 'İ', 'ÄŸ': 'ğ', 'Ä±': 'ı', 'ÅŸ': 'ş',
    }
    for wrong, right in replacements.items():
        text = text.replace(wrong, right)
    return text


def truncate_content(text: str, max_chars: int = 4000) -> str:
    """
    LLM prompt boyutunu kontrol altında tutmak için içeriği kırp.
    Cümle ortasında kesmemeye çalışır.
    """
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_period = max(truncated.rfind('.'), truncated.rfind('!'), truncated.rfind('?'))
    if last_period > max_chars * 0.7:
        return truncated[:last_period + 1]
    return truncated + "..."


def is_duplicate(content: str, existing_contents: list[str], threshold: float = 0.85) -> bool:
    """
    Jaccard benzerliğiyle basit kopya tespiti.
    İki içeriğin token setlerinin kesişimi / birleşimi eşiği aşarsa True döner.
    """
    tokens_new = set(content.lower().split())
    for existing in existing_contents:
        tokens_ex = set(existing.lower().split())
        if not tokens_new or not tokens_ex:
            continue
        intersection = tokens_new & tokens_ex
        union = tokens_new | tokens_ex
        similarity = len(intersection) / len(union)
        if similarity >= threshold:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Ana temizleme işlemi
# ─────────────────────────────────────────────────────────────────────────────

def clean_all_records(dry_run: bool = False) -> dict:
    """
    Veritabanındaki tüm aktif UniversityContent kayıtlarını temizler.

    dry_run=True: Değişiklikleri kaydetmez, sadece rapor üretir.
    Döndürdüğü dict: {processed, updated, deactivated, duplicates_removed}
    """
    stats = {"processed": 0, "updated": 0, "deactivated": 0, "duplicates_removed": 0}

    records = UniversityContent.objects.filter(is_active=True).order_by('scraped_at')
    log.info("%d aktif kayıt temizlenecek.", records.count())

    seen_contents: list[str] = []

    for rec in records:
        stats["processed"] += 1
        original_content = rec.content
        original_title   = rec.title

        # 1. Encoding düzeltme
        content = fix_turkish_encoding(rec.content)
        title   = fix_turkish_encoding(rec.title)

        # 2. Gürültü kaldırma
        content = remove_noise(content)
        content = remove_boilerplate(content)

        # 3. Boşluk normalizasyonu
        content = normalize_whitespace(content)
        title   = normalize_whitespace(title)

        # 4. Çok kısa içerikleri devre dışı bırak
        if len(content.strip()) < 50:
            log.info("Çok kısa içerik devre dışı: %s", rec.url)
            if not dry_run:
                rec.is_active = False
                rec.save(update_fields=['is_active'])
            stats["deactivated"] += 1
            continue

        # 5. İçeriği kırp
        content = truncate_content(content)

        # 6. Kopya kontrolü
        if is_duplicate(content, seen_contents):
            log.info("Kopya devre dışı: %s", rec.url)
            if not dry_run:
                rec.is_active = False
                rec.save(update_fields=['is_active'])
            stats["duplicates_removed"] += 1
            continue

        seen_contents.append(content)

        # 7. Değişiklik varsa kaydet
        if content != original_content or title != original_title:
            rec.content = content
            rec.title   = title
            if not dry_run:
                rec.save(update_fields=['content', 'title', 'updated_at'])
            stats["updated"] += 1

    log.info(
        "Temizleme tamamlandı: %d işlendi, %d güncellendi, "
        "%d devre dışı, %d kopya kaldırıldı%s.",
        stats["processed"], stats["updated"],
        stats["deactivated"], stats["duplicates_removed"],
        " (DRY RUN)" if dry_run else "",
    )
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ACU veri temizleyici")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Değişiklikleri kaydetmeden sadece rapor üret."
    )
    args = parser.parse_args()
    clean_all_records(dry_run=args.dry_run)

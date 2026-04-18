"""
scraper/bs4_scraper.py
──────────────────────
Acıbadem Üniversitesi statik sayfaları için BeautifulSoup tabanlı scraper.
Dinamik (JS-render) sayfalar için selenium_scraper.py kullanın.

Çalıştırmak için (Django'nun manage.py dizininden):
    python scraper/bs4_scraper.py
veya Docker içinde:
    docker compose exec web python scraper/bs4_scraper.py
"""

import os
import sys
import time
import logging
import django
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime

# ── Django kurulumu ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from chat.models import UniversityContent, Faculty, Department, Course, ScraperLog

# ── Logger ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Sabit ayarlar ─────────────────────────────────────────────────────────────
BASE_URL   = "https://www.acibadem.edu.tr"
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ACU-ChatBot-Scraper/1.0; "
        "+https://github.com/mahirfurkandalyan/ACU-CHATBOT)"
    )
}
REQUEST_DELAY = 1.5   # saniye – sunucuyu yormamak için
MAX_RETRIES   = 3


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcı fonksiyonlar
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    """Verilen URL'i indir, BeautifulSoup nesnesi döndür. Hata olursa None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            log.warning("Deneme %d/%d başarısız: %s – %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)
    return None


def clean_text(text: str) -> str:
    """Fazla boşlukları ve özel karakterleri temizler."""
    import re
    text = re.sub(r'\s+', ' ', text)          # çoklu boşluk → tek boşluk
    text = re.sub(r'\xa0', ' ', text)         # non-breaking space
    text = re.sub(r'[\u200b\u200c\u200d]', '', text)  # sıfır genişlikli karakterler
    return text.strip()


def detect_language(url: str) -> str:
    """URL'de /en/ geçiyorsa İngilizce, yoksa Türkçe."""
    return "en" if "/en/" in url else "tr"


def url_to_category(url: str) -> str:
    """URL yapısına bakarak içerik kategorisini tahmin et."""
    url_lower = url.lower()
    if any(k in url_lower for k in ["/program", "/bolum", "/department", "/lisans", "/yuksek-lisans"]):
        return "academic"
    if any(k in url_lower for k in ["/ders", "/course", "/katalog"]):
        return "course"
    if any(k in url_lower for k in ["/kampus", "/campus", "/tesis", "/facility"]):
        return "campus"
    if any(k in url_lower for k in ["/fakulte", "/faculty", "/enstitu", "/institute"]):
        return "faculty"
    if any(k in url_lower for k in ["/kayit", "/kabul", "/admission", "/basvur"]):
        return "admission"
    if any(k in url_lower for k in ["/arastirma", "/research", "/proje"]):
        return "research"
    if any(k in url_lower for k in ["/haber", "/news", "/duyuru", "/announcement"]):
        return "news"
    if any(k in url_lower for k in ["/iletisim", "/contact"]):
        return "contact"
    return "other"


def save_content(title: str, content: str, url: str, category: str = "other") -> bool:
    """
    UniversityContent tablosuna kayıt ekler.
    Aynı URL zaten varsa günceller (upsert).
    Boş içerik kaydetmez.
    """
    content = clean_text(content)
    title   = clean_text(title)

    if not content or len(content) < 30:
        log.debug("İçerik çok kısa, atlandı: %s", url)
        return False

    obj, created = UniversityContent.objects.update_or_create(
        url=url,
        defaults={
            "title":    title or "Başlıksız",
            "content":  content,
            "category": category,
            "language": detect_language(url),
            "is_active": True,
        },
    )
    action = "Eklendi" if created else "Güncellendi"
    log.info("%s: %s", action, title[:60])
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Sayfa ayrıştırıcıları
# ─────────────────────────────────────────────────────────────────────────────

def parse_generic_page(soup: BeautifulSoup, url: str) -> int:
    """
    Genel sayfa ayrıştırıcısı.
    Ana içerik alanını (<main>, <article> veya .content sınıfını) bulur,
    başlık + paragraf metnini çeker.
    """
    saved = 0

    # Başlık
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(separator=" ") if title_tag else url

    # Ana içerik bloğunu bul
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(class_=lambda c: c and any(x in c for x in ["content", "main", "page-body"]))
        or soup.find("body")
    )

    if not main:
        return 0

    # Gereksiz elemanları kaldır
    for tag in main.find_all(["nav", "header", "footer", "script", "style", "noscript"]):
        tag.decompose()

    # Paragrafları birleştir
    paragraphs = main.find_all(["p", "li", "td", "h2", "h3", "h4"])
    text_parts = [p.get_text(separator=" ") for p in paragraphs if p.get_text(strip=True)]
    content = " ".join(text_parts)

    category = url_to_category(url)
    if save_content(title, content, url, category):
        saved += 1

    return saved


def parse_faculty_page(soup: BeautifulSoup, url: str) -> int:
    """Fakülte sayfasından fakülte ve bölüm bilgilerini çeker."""
    saved = 0

    h1 = soup.find("h1")
    faculty_name = clean_text(h1.get_text()) if h1 else "Bilinmeyen Fakülte"

    faculty, _ = Faculty.objects.get_or_create(
        name=faculty_name,
        defaults={"url": url},
    )

    # Bölüm listelerini ara
    dept_headings = soup.find_all(["h2", "h3", "h4"])
    for heading in dept_headings:
        dept_name = clean_text(heading.get_text())
        if not dept_name or len(dept_name) < 5:
            continue
        # Bölüm adında anlamlı anahtar kelimeler olsun
        if any(k in dept_name.lower() for k in ["bölüm", "program", "department", "engineering", "mühendis"]):
            Department.objects.get_or_create(
                faculty=faculty,
                name=dept_name,
                defaults={"url": url},
            )

    # Genel içerik de kaydet
    saved += parse_generic_page(soup, url)
    return saved


# ─────────────────────────────────────────────────────────────────────────────
# Scraper görevleri
# ─────────────────────────────────────────────────────────────────────────────

# Taranacak URL listesi (kategori bilgisiyle birlikte)
SEED_URLS = [
    # Ana sayfa
    (f"{BASE_URL}/tr", "other"),
    (f"{BASE_URL}/en", "other"),

    # Hakkında
    (f"{BASE_URL}/tr/kurumsal/hakkimizda", "other"),

    # Fakülteler
    (f"{BASE_URL}/tr/akademik/fakulteler", "faculty"),
    (f"{BASE_URL}/tr/akademik/fakulteler/tip-fakultesi", "faculty"),
    (f"{BASE_URL}/tr/akademik/fakulteler/muhendislik-ve-dogal-bilimler-fakultesi", "faculty"),
    (f"{BASE_URL}/tr/akademik/fakulteler/saglik-bilimleri-fakultesi", "faculty"),
    (f"{BASE_URL}/tr/akademik/enstituler", "faculty"),

    # Lisans Programları
    (f"{BASE_URL}/tr/akademik/lisans-programlari", "academic"),
    (f"{BASE_URL}/tr/akademik/yuksek-lisans-ve-doktora-programlari", "academic"),

    # Kampüs
    (f"{BASE_URL}/tr/kampus-hayati", "campus"),
    (f"{BASE_URL}/tr/kampus-hayati/tesisler", "campus"),
    (f"{BASE_URL}/tr/kampus-hayati/yurtlar", "campus"),

    # Kayıt
    (f"{BASE_URL}/tr/ogrenci-kabul", "admission"),
    (f"{BASE_URL}/tr/ogrenci-kabul/lisans-basvurusu", "admission"),
    (f"{BASE_URL}/tr/ogrenci-kabul/yuksek-lisans-basvurusu", "admission"),

    # Araştırma
    (f"{BASE_URL}/tr/arastirma", "research"),

    # İletişim
    (f"{BASE_URL}/tr/iletisim", "contact"),
]


def run_static_scraper():
    """Tüm seed URL'leri tara, sonuçları kaydet."""
    total_saved = 0

    for url, forced_category in SEED_URLS:
        log.info("Taranıyor: %s", url)
        start = time.time()

        soup = fetch_page(url)
        if soup is None:
            ScraperLog.objects.create(
                url=url, status="failed",
                error_message="Sayfa indirilemedi.",
                duration_seconds=time.time() - start,
            )
            continue

        try:
            if "fakult" in url.lower() or "faculty" in url.lower():
                saved = parse_faculty_page(soup, url)
            else:
                saved = parse_generic_page(soup, url)

            total_saved += saved
            ScraperLog.objects.create(
                url=url,
                status="success" if saved > 0 else "partial",
                records_saved=saved,
                duration_seconds=time.time() - start,
            )
        except Exception as exc:
            log.error("Ayrıştırma hatası (%s): %s", url, exc)
            ScraperLog.objects.create(
                url=url, status="failed",
                error_message=str(exc),
                duration_seconds=time.time() - start,
            )

        time.sleep(REQUEST_DELAY)

    log.info("Tamamlandı. Toplam %d kayıt kaydedildi.", total_saved)
    return total_saved


if __name__ == "__main__":
    log.info("ACU BS4 Scraper başlatılıyor...")
    run_static_scraper()

"""
scraper/bologna_scraper.py
───────────────────────────
Acıbadem Üniversitesi Bologna Bilgi Paketi (OBS) scraper.
URL: https://obs.acibadem.edu.tr/oibs/bologna/index.aspx

Bu site ASP.NET tabanlı ve içerik JavaScript ile yüklenir.
Bu yüzden Selenium ile headless Chrome kullanılır.

Çekilen veriler:
  - Kurumsal Bilgiler (Yönetim, Üniversite Hakkında, Bologna Komisyonu, İletişim, AKTS)
  - Akademik Birimler: Ön Lisans / Lisans / Yüksek Lisans / Doktora programları
    → Her program için: adı, kodu, bölüm bilgisi, AKTS kredisi
    → Her program içinde ders listesi (kod, ad, kredi, AKTS, dönem, zorunlu/seçmeli)
  - Öğrenciler İçin Genel Bilgiler (Kampüs, Şehir, Konaklama, Sağlık vb.)
  - Erasmus Beyannamesi
  - Bologna Süreci

Çalıştırma:
    python scraper/bologna_scraper.py
    python scraper/bologna_scraper.py --lang en   # İngilizce versiyon
    python scraper/bologna_scraper.py --lang both  # Her ikisi
"""

import os
import sys
import time
import logging
import argparse
import django

# ── Django kurulumu ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from chat.models import UniversityContent, Faculty, Department, Course, ScraperLog
from scraper.bs4_scraper import clean_text, save_content

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ── Selenium import ─────────────────────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
        StaleElementReferenceException, ElementNotInteractableException,
    )
    from bs4 import BeautifulSoup
except ImportError as e:
    log.error("Eksik bağımlılık: %s  →  pip install selenium webdriver-manager beautifulsoup4", e)
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Sabitler
# ─────────────────────────────────────────────────────────────────────────────

BOLOGNA_BASE = "https://obs.acibadem.edu.tr/oibs/bologna/index.aspx"
PAGE_WAIT    = 15   # saniye – eleman bekleme limiti
CLICK_DELAY  = 1.5  # tıklama sonrası bekleme
SCROLL_PAUSE = 0.8

# Menü yapısı: (menü metni, kategori, dil-bağımsız anahtar)
MENU_STRUCTURE = {
    "tr": {
        "kurumsal": [
            ("Yönetim",                "other"),
            ("Üniversite Hakkında",    "other"),
            ("Bologna Komisyonu",      "other"),
            ("İletişim",              "contact"),
            ("AKTS Kataloğu",         "academic"),
        ],
        "akademik": [
            ("Ön Lisans",      "academic"),
            ("Lisans",         "academic"),
            ("Yüksek Lisans",  "academic"),
            ("Doktora",        "academic"),
        ],
        "ogrenci": [
            ("Şehir Hakkında",           "campus"),
            ("Kampüs",                   "campus"),
            ("Yemek",                    "campus"),
            ("Sağlık Hizmetleri",        "campus"),
            ("Spor ve Sosyal Yaşam",     "campus"),
            ("Öğrenci Kulüpleri",        "campus"),
            ("Konaklama",                "campus"),
            ("Engelli Öğrenci Hizmetleri", "campus"),
        ],
        "erasmus": [
            ("Erasmus+ Beyannamesi",  "other"),
        ],
        "bologna": [
            ("Bologna Süreci",  "other"),
        ],
    },
    "en": {
        "kurumsal": [
            ("Management",                "other"),
            ("About University",          "other"),
            ("Bologna Commission",        "other"),
            ("Contact",                   "contact"),
            ("ECTS Catalogue",            "academic"),
        ],
        "akademik": [
            ("Associate Degree",  "academic"),
            ("Bachelor's Degree", "academic"),
            ("Master's Degree",   "academic"),
            ("PhD",               "academic"),
        ],
        "ogrenci": [
            ("About the City",            "campus"),
            ("Campus",                    "campus"),
            ("Food",                      "campus"),
            ("Health Services",           "campus"),
            ("Sports and Social Life",    "campus"),
            ("Student Clubs",             "campus"),
            ("Accommodation",             "campus"),
            ("Disabled Student Services", "campus"),
        ],
        "erasmus": [
            ("Erasmus+ Charter",  "other"),
        ],
        "bologna": [
            ("Bologna Process",  "other"),
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────

def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=tr-TR")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=opts)
    except ImportError:
        return webdriver.Chrome(options=opts)


def wait_for(driver, css: str, timeout: int = PAGE_WAIT):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css))
        )
    except TimeoutException:
        return None


def scroll_page(driver):
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(SCROLL_PAUSE)
    driver.execute_script("window.scrollTo(0, 0);")


def get_page_text(driver) -> str:
    """Sayfanın görünür metin içeriğini döndürür (nav/script hariç)."""
    soup = BeautifulSoup(driver.page_source, "html.parser")
    for tag in soup.find_all(["nav", "script", "style", "noscript", "header", "footer"]):
        tag.decompose()
    main = soup.find("div", id=lambda x: x and "icerik" in x.lower())
    if not main:
        main = soup.find("main") or soup.find("div", class_=lambda c: c and "content" in c.lower())
    if not main:
        main = soup.find("body")
    return clean_text(main.get_text(separator=" ")) if main else ""


# ─────────────────────────────────────────────────────────────────────────────
# Menü tıklama yardımcıları
# ─────────────────────────────────────────────────────────────────────────────

def click_menu_item(driver, link_text: str) -> bool:
    """
    Navbarda verilen metni içeren linki bulup tıklar.
    Exact match, sonra partial match dener.
    """
    # Önce tam eşleşme dene
    for by, val in [
        (By.LINK_TEXT, link_text),
        (By.PARTIAL_LINK_TEXT, link_text[:15]),
        (By.XPATH, f"//a[contains(normalize-space(.), '{link_text[:20]}')]"),
    ]:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, val)))
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            time.sleep(0.3)
            el.click()
            time.sleep(CLICK_DELAY)
            return True
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException,
                ElementNotInteractableException):
            continue
    log.warning("Menü öğesi bulunamadı: '%s'", link_text)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Akademik birim tarayıcısı (Ön Lisans / Lisans / YL / Doktora)
# ─────────────────────────────────────────────────────────────────────────────

def scrape_academic_level(driver, level_text: str, lang: str) -> int:
    """
    Bir akademik seviye (Lisans vb.) menüsüne tıklar.
    Açılan program listesini iter eder, her programın ders listesini çeker.
    """
    saved = 0
    base_url = f"{BOLOGNA_BASE}?lang={lang}"

    if not click_menu_item(driver, level_text):
        return 0

    time.sleep(2)

    # Program seçme dropdown var mı?
    program_select = None
    for sel_id in ["ctl00_ContentPlaceHolder1_ddlBirim", "ddlBirim", "ddlProgram",
                   "ctl00_ContentPlaceHolder1_ddlProgram"]:
        try:
            program_select = Select(driver.find_element(By.ID, sel_id))
            log.info("Program dropdown bulundu: id=%s (%d seçenek)", sel_id, len(program_select.options))
            break
        except NoSuchElementException:
            continue

    if program_select:
        # Dropdown'daki her programı tara
        option_count = len(program_select.options)
        for i in range(1, option_count):   # 0 = "Seçiniz" satırı
            try:
                # Her iterasyonda dropdown'u yeniden bul (DOM yenilenebilir)
                select_el = None
                for sel_id in ["ctl00_ContentPlaceHolder1_ddlBirim", "ddlBirim", "ddlProgram",
                               "ctl00_ContentPlaceHolder1_ddlProgram"]:
                    try:
                        select_el = Select(driver.find_element(By.ID, sel_id))
                        break
                    except NoSuchElementException:
                        continue
                if not select_el:
                    break

                opt = select_el.options[i]
                prog_name  = clean_text(opt.text)
                prog_value = opt.get_attribute("value")

                if not prog_name or prog_name.lower() in ("seçiniz", "select"):
                    continue

                log.info("  Program: %s", prog_name)
                select_el.select_by_index(i)
                time.sleep(2)

                # Ders tablosunu çek
                saved += _extract_program_courses(driver, prog_name, level_text, lang)

            except (StaleElementReferenceException, Exception) as exc:
                log.debug("Program iterasyon hatası (i=%d): %s", i, exc)
                continue
    else:
        # Dropdown yok; sayfanın genel içeriğini kaydet
        text = get_page_text(driver)
        if text:
            title = f"{level_text} – Programlar"
            url   = driver.current_url
            save_content(title, text, url, "academic")
            saved += 1

    return saved


def _extract_program_courses(driver, prog_name: str, level: str, lang: str) -> int:
    """
    Aktif program sayfasından ders tablosunu çeker.
    Dersleri hem Course modeline hem de UniversityContent'e kaydeder.
    """
    saved = 0
    url   = driver.current_url

    # Ders tablosu seçicileri (site yapısına göre)
    TABLE_SELECTORS = [
        "table.ders-tablosu",
        "table.table",
        "#ctl00_ContentPlaceHolder1_gvDersler",
        "table",
    ]

    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = None
    for sel in TABLE_SELECTORS:
        table = soup.select_one(sel)
        if table:
            break

    if not table:
        # Tablo yoksa sayfanın tüm metnini kaydet
        text = get_page_text(driver)
        if text:
            save_content(f"{level}: {prog_name}", text, url, "academic")
            saved += 1
        return saved

    rows = table.find_all("tr")
    header = [clean_text(th.get_text()) for th in (rows[0].find_all("th") if rows else [])]
    log.debug("Tablo başlıkları: %s", header)

    # Başlık sütunlarını tahmin et
    def col_index(keywords):
        for i, h in enumerate(header):
            if any(k in h.lower() for k in keywords):
                return i
        return None

    idx_code   = col_index(["kod", "code"])
    idx_name   = col_index(["ad", "name", "ders", "course"])
    idx_akts   = col_index(["akts", "ects"])
    idx_kredi  = col_index(["kredi", "credit"])
    idx_donem  = col_index(["dönem", "semester", "yarıyıl"])
    idx_tur    = col_index(["tür", "type", "zorunlu", "seçmeli"])

    course_lines = []

    for row in rows[1:]:
        cols = row.find_all(["td", "th"])
        if len(cols) < 2:
            continue

        def cell(idx):
            if idx is not None and idx < len(cols):
                return clean_text(cols[idx].get_text())
            return ""

        code   = cell(idx_code)
        name   = cell(idx_name) or cell(1)
        akts   = cell(idx_akts)
        kredi  = cell(idx_kredi)
        donem  = cell(idx_donem)
        tur    = cell(idx_tur)

        if not name:
            continue

        # Faculty / Department bul veya oluştur
        faculty, _ = Faculty.objects.get_or_create(
            name=prog_name,
            defaults={"url": url},
        )
        dept, _ = Department.objects.get_or_create(
            faculty=faculty,
            name=f"{level} – {prog_name}",
            defaults={"url": url},
        )

        # Course modeline kaydet
        try:
            credits_int = int(kredi) if kredi and kredi.isdigit() else None
        except ValueError:
            credits_int = None

        Course.objects.update_or_create(
            code=code,
            name=name,
            defaults={
                "department":  dept,
                "description": f"AKTS: {akts}  Tür: {tur}",
                "credits":     credits_int,
                "semester":    donem,
                "url":         url,
            },
        )

        line = f"Kod: {code} | Ders: {name} | AKTS: {akts} | Kredi: {kredi} | Dönem: {donem} | Tür: {tur}"
        course_lines.append(line.strip(" |"))

    if course_lines:
        content = f"Program: {prog_name}\nSeviye: {level}\n\n" + "\n".join(course_lines)
        save_content(f"Ders Listesi – {prog_name}", content, url, "course")
        saved += 1
        log.info("    %d ders kaydedildi.", len(course_lines))

    return saved


# ─────────────────────────────────────────────────────────────────────────────
# Genel içerik sayfası tarayıcısı
# ─────────────────────────────────────────────────────────────────────────────

def scrape_info_page(driver, page_name: str, category: str, lang: str) -> int:
    """
    Kurumsal veya öğrenci bilgisi sayfasını açar ve içeriği kaydeder.
    """
    if not click_menu_item(driver, page_name):
        return 0

    time.sleep(1.5)
    scroll_page(driver)

    title   = f"{page_name}"
    content = get_page_text(driver)
    url     = driver.current_url

    if not content or len(content) < 40:
        log.debug("Boş sayfa: %s", page_name)
        return 0

    save_content(title, content, url, category)
    log.info("Kaydedildi: %s (%d karakter)", page_name, len(content))
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Ana çalıştırıcı
# ─────────────────────────────────────────────────────────────────────────────

def run_bologna_scraper(lang: str = "tr") -> int:
    """
    Bologna OBS sitesini baştan sona tarar.
    lang: 'tr' veya 'en'
    """
    url = f"{BOLOGNA_BASE}?lang={lang}"
    log.info("Bologna OBS scraper başlatılıyor → %s", url)

    driver = build_driver()
    total  = 0
    start  = __import__("time").time()

    try:
        driver.get(url)
        time.sleep(3)   # ilk yüklenme

        menu = MENU_STRUCTURE.get(lang, MENU_STRUCTURE["tr"])

        # ── 1. Kurumsal bilgiler ──────────────────────────────────────────
        log.info("─── Kurumsal Bilgiler ───")
        for page_name, category in menu["kurumsal"]:
            driver.get(url)       # sayfayı sıfırla
            time.sleep(1.5)
            # Ana menüde "Kurumsal Bilgiler" hover/tıkla
            click_menu_item(driver, "Kurumsal Bilgiler" if lang == "tr" else "Institutional Information")
            time.sleep(0.8)
            total += scrape_info_page(driver, page_name, category, lang)

        # ── 2. Akademik birimler ──────────────────────────────────────────
        log.info("─── Akademik Birimler ───")
        for level_text, _ in menu["akademik"]:
            driver.get(url)
            time.sleep(1.5)
            click_menu_item(driver, "Akademik Birimler" if lang == "tr" else "Academic Units")
            time.sleep(0.8)
            total += scrape_academic_level(driver, level_text, lang)

        # ── 3. Öğrenciler için genel bilgiler ────────────────────────────
        log.info("─── Öğrenciler İçin Bilgiler ───")
        for page_name, category in menu["ogrenci"]:
            driver.get(url)
            time.sleep(1.5)
            click_menu_item(
                driver,
                "Öğrenciler İçin Genel Bilgiler" if lang == "tr" else "General Information for Students"
            )
            time.sleep(0.8)
            total += scrape_info_page(driver, page_name, category, lang)

        # ── 4. Erasmus ────────────────────────────────────────────────────
        log.info("─── Erasmus ───")
        for page_name, category in menu["erasmus"]:
            driver.get(url)
            time.sleep(1.5)
            click_menu_item(driver, "Erasmus Beyannamesi" if lang == "tr" else "Erasmus Charter")
            time.sleep(0.8)
            total += scrape_info_page(driver, page_name, category, lang)

        # ── 5. Bologna süreci ─────────────────────────────────────────────
        log.info("─── Bologna Süreci ───")
        for page_name, category in menu["bologna"]:
            driver.get(url)
            time.sleep(1.5)
            click_menu_item(driver, "Bologna Süreci" if lang == "tr" else "Bologna Process")
            time.sleep(0.8)
            total += scrape_info_page(driver, page_name, category, lang)

        # ── Log ───────────────────────────────────────────────────────────
        ScraperLog.objects.create(
            url=url,
            status="success" if total > 0 else "partial",
            records_saved=total,
            duration_seconds=__import__("time").time() - start,
        )

    except Exception as exc:
        log.error("Bologna scraper genel hata: %s", exc, exc_info=True)
        ScraperLog.objects.create(
            url=url, status="failed",
            error_message=str(exc),
            duration_seconds=__import__("time").time() - start,
        )
    finally:
        driver.quit()

    log.info("Bologna OBS tamamlandı. Toplam %d kayıt (%s).", total, lang.upper())
    return total


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ACU Bologna OBS Scraper")
    parser.add_argument(
        "--lang",
        choices=["tr", "en", "both"],
        default="tr",
        help="Hangi dil versiyonu taransın? (varsayılan: tr)",
    )
    args = parser.parse_args()

    if args.lang == "both":
        run_bologna_scraper("tr")
        run_bologna_scraper("en")
    else:
        run_bologna_scraper(args.lang)

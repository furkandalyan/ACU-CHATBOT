"""
scraper/selenium_scraper.py
────────────────────────────
JS ile render edilen dinamik sayfalar için Selenium tabanlı scraper.
Özellikle ders kataloğu veya arama sonuçları gibi sayfalar için kullanılır.

Gereksinimler (requirements.txt'e ekleyin):
    selenium==4.20.0
    webdriver-manager==4.0.1

Chrome kurulu olmalıdır. Docker'da headless modda çalışır.

Kullanım:
    python scraper/selenium_scraper.py
"""

import os
import sys
import time
import logging
import django

# ── Django kurulumu ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from chat.models import UniversityContent, Course, Department, ScraperLog
from scraper.bs4_scraper import clean_text, save_content, detect_language

# ── Logger ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Selenium import (opsiyonel bağımlılık) ─────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    log.warning("Selenium kurulu değil. 'pip install selenium webdriver-manager' çalıştırın.")

BASE_URL      = "https://www.acibadem.edu.tr"
PAGE_TIMEOUT  = 20   # saniye
SCROLL_PAUSE  = 1.5  # sayfa kaydırma arası bekleme
REQUEST_DELAY = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Driver yönetimi
# ─────────────────────────────────────────────────────────────────────────────

def build_driver() -> "webdriver.Chrome":
    """Headless Chrome WebDriver oluşturur."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )

    # webdriver-manager varsa otomatik driver indir
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=opts)
    except ImportError:
        # webdriver-manager yoksa PATH'teki chromedriver kullan
        return webdriver.Chrome(options=opts)


def scroll_to_bottom(driver, pause: float = SCROLL_PAUSE):
    """Sayfayı yavaşça aşağı kaydırarak lazy-load içerikleri tetikler."""
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height


def wait_for_element(driver, css_selector: str, timeout: int = PAGE_TIMEOUT):
    """Belirtilen CSS seçicinin yüklenmesini bekler."""
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css_selector))
        )
    except TimeoutException:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Sayfa görevleri
# ─────────────────────────────────────────────────────────────────────────────

def scrape_course_catalog(driver) -> int:
    """
    Ders kataloğu sayfasını Selenium ile tarar.
    JS ile render edilen ders kartlarını yakalar.
    """
    url   = f"{BASE_URL}/tr/akademik/ders-katalog"
    saved = 0
    start = time.time()

    log.info("Ders kataloğu taranıyor: %s", url)

    try:
        driver.get(url)
        time.sleep(3)  # ilk yüklenme için bekle
        scroll_to_bottom(driver)

        # Ders kartlarını bul – gerçek selector site yapısına göre güncellenmeli
        course_cards = driver.find_elements(By.CSS_SELECTOR, ".course-card, .ders-karti, article.course")

        if not course_cards:
            # Alternatif selector: tablo satırları
            course_cards = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

        log.info("%d ders kartı/satırı bulundu.", len(course_cards))

        for card in course_cards:
            try:
                # Başlık
                try:
                    name_el = card.find_element(By.CSS_SELECTOR, "h2, h3, .course-name, td:nth-child(2)")
                    name = clean_text(name_el.text)
                except NoSuchElementException:
                    name = ""

                # Kod
                try:
                    code_el = card.find_element(By.CSS_SELECTOR, ".course-code, .kod, td:nth-child(1)")
                    code = clean_text(code_el.text)
                except NoSuchElementException:
                    code = ""

                # Açıklama
                try:
                    desc_el = card.find_element(By.CSS_SELECTOR, ".course-desc, .aciklama, p")
                    desc = clean_text(desc_el.text)
                except NoSuchElementException:
                    desc = ""

                if not name:
                    continue

                # Course modeline kaydet
                Course.objects.update_or_create(
                    code=code,
                    name=name,
                    defaults={"description": desc, "url": url},
                )

                # UniversityContent'e de kaydet (RAG için)
                content = f"Ders Kodu: {code}\nDers Adı: {name}\nAçıklama: {desc}"
                save_content(
                    title=f"Ders: {code} {name}".strip(),
                    content=content,
                    url=url,
                    category="course",
                )
                saved += 1

            except Exception as exc:
                log.debug("Ders kartı ayrıştırma hatası: %s", exc)
                continue

        ScraperLog.objects.create(
            url=url, status="success" if saved > 0 else "partial",
            records_saved=saved,
            duration_seconds=time.time() - start,
        )

    except Exception as exc:
        log.error("Ders kataloğu hatası: %s", exc)
        ScraperLog.objects.create(
            url=url, status="failed",
            error_message=str(exc),
            duration_seconds=time.time() - start,
        )

    log.info("Ders kataloğu: %d kayıt.", saved)
    return saved


def scrape_dynamic_pages(driver) -> int:
    """
    Diğer JS-render sayfaları için genel Selenium tarayıcı.
    İçerik bloğunu bekler, metni çekip kayıt eder.
    """
    DYNAMIC_URLS = [
        (f"{BASE_URL}/tr/arastirma/projeler", "research"),
        (f"{BASE_URL}/tr/haberler", "news"),
        (f"{BASE_URL}/tr/etkinlikler", "news"),
    ]

    total = 0
    for url, category in DYNAMIC_URLS:
        start = time.time()
        log.info("Dinamik sayfa: %s", url)
        try:
            driver.get(url)
            time.sleep(3)
            scroll_to_bottom(driver)

            # Sayfa başlığı
            try:
                title = clean_text(driver.find_element(By.TAG_NAME, "h1").text)
            except NoSuchElementException:
                title = driver.title or url

            # Tüm görünür metin
            try:
                body = driver.find_element(By.TAG_NAME, "main") or driver.find_element(By.TAG_NAME, "body")
                content = clean_text(body.text)
            except NoSuchElementException:
                content = clean_text(driver.find_element(By.TAG_NAME, "body").text)

            saved = 1 if save_content(title, content, url, category) else 0
            total += saved
            ScraperLog.objects.create(
                url=url, status="success" if saved else "partial",
                records_saved=saved,
                duration_seconds=time.time() - start,
            )
        except Exception as exc:
            log.error("Dinamik sayfa hatası (%s): %s", url, exc)
            ScraperLog.objects.create(
                url=url, status="failed",
                error_message=str(exc),
                duration_seconds=time.time() - start,
            )
        time.sleep(REQUEST_DELAY)

    return total


# ─────────────────────────────────────────────────────────────────────────────
# Ana çalıştırıcı
# ─────────────────────────────────────────────────────────────────────────────

def run_selenium_scraper():
    if not SELENIUM_AVAILABLE:
        log.error("Selenium kurulu değil. Çıkılıyor.")
        return 0

    log.info("Selenium scraper başlatılıyor (headless Chrome)...")
    driver = build_driver()
    total = 0

    try:
        total += scrape_course_catalog(driver)
        total += scrape_dynamic_pages(driver)
    finally:
        driver.quit()
        log.info("Driver kapatıldı. Toplam %d kayıt.", total)

    return total


if __name__ == "__main__":
    run_selenium_scraper()

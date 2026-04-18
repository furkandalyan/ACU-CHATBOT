"""
scraper/run_all.py
───────────────────
Tüm scraping + temizleme adımlarını sırayla çalıştırır.

Kullanım:
    python scraper/run_all.py               # hepsini çalıştır
    python scraper/run_all.py --only bs4     # sadece BeautifulSoup scraper
    python scraper/run_all.py --only sel     # sadece Selenium scraper
    python scraper/run_all.py --only bologna # sadece Bologna OBS scraper
    python scraper/run_all.py --only clean   # sadece veri temizleyici
"""

import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="ACU Chatbot – Tam Scraping Çalıştırıcı")
    parser.add_argument(
        "--only",
        choices=["bs4", "sel", "bologna", "clean"],
        default=None,
        help="Yalnızca belirli bir adımı çalıştır.",
    )
    args = parser.parse_args()

    if args.only in (None, "bs4"):
        log.info("─── BeautifulSoup Scraper ───")
        from scraper.bs4_scraper import run_static_scraper
        count = run_static_scraper()
        log.info("BS4 tamamlandı: %d kayıt.", count)

    if args.only in (None, "sel"):
        log.info("─── Selenium Scraper ───")
        from scraper.selenium_scraper import run_selenium_scraper
        count = run_selenium_scraper()
        log.info("Selenium tamamlandı: %d kayıt.", count)

    if args.only in (None, "bologna"):
        log.info("─── Bologna OBS Scraper ───")
        from scraper.bologna_scraper import run_bologna_scraper
        count  = run_bologna_scraper("tr")
        count += run_bologna_scraper("en")
        log.info("Bologna tamamlandı: %d kayıt.", count)

    if args.only in (None, "clean"):
        log.info("─── Veri Temizleyici ───")
        from scraper.data_cleaner import clean_all_records
        stats = clean_all_records()
        log.info("Temizlik bitti: %s", stats)

    log.info("Tüm işlemler tamamlandı.")


if __name__ == "__main__":
    main()

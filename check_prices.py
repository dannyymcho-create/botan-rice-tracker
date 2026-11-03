import os
import re
import json
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request
import urllib.parse
from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

STORES = [
    {
        "name": "Walmart Canada",
        "key": "walmart",
        "url": "https://www.walmart.ca/en/ip/Botan-Calrose-rice-8kg/6000188764120?classType=REGULAR&athbdg=L1103&from=/search",
        "flipp_keywords": ["walmart"]
    },
    {
        "name": "Food Basics",
        "key": "foodbasics",
        "url": "https://www.foodbasics.ca/search?filter=botan+calrose",
        "direct_url": "https://www.foodbasics.ca/aisles/pantry/pasta-rice-beans/rice/calrose-rice/p/061414010801",
        "flipp_keywords": ["food basics", "foodbasics"]
    },
    {
        "name": "Loblaws",
        "key": "loblaws",
        "url": "https://www.loblaws.ca/en/calrose-rice/p/20428552_EA",
        "flipp_keywords": ["loblaws", "loblaw"]
    }
]

POSTAL_CODE = os.environ.get("POSTAL_CODE", "K2J6C8").replace(" ", "")


def check_flipp_flyers(postal_code: str) -> list:
    deals = []
    try:
        query = urllib.parse.quote("botan calrose")
        url = f"https://backflipp.wishabi.com/flipp/items/search?locale=en-ca&postal_code={postal_code}&q={query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            for item in items:
                name = (item.get("name") or "").strip()
                merchant = (item.get("merchant_name") or "").strip()
                price = item.get("current_price")
                valid_from = item.get("valid_from", "")[:10]
                valid_to = item.get("valid_to", "")[:10]
                if "botan" in name.lower() or "calrose" in name.lower():
                    deals.append({
                        "merchant": merchant,
                        "name": name,
                        "price": f"${price}" if price else "전단지 확인",
                        "period": f"{valid_from} ~ {valid_to}" if valid_to else ""
                    })
    except Exception as e:
        print(f"[경고] Flipp 전단지 조회 중 오류: {e}")
    return deals


def parse_price_from_html_text(html: str) -> tuple:
    if not html or len(html) < 500:
        return None, False

    m_next = re.search(r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*(\d+\.?\d*)', html)
    if not m_next:
        m_next = re.search(r'"priceInfo"\s*:.*?"price"\s*:\s*(\d+\.\d{2})', html)
    if m_next:
        val = float(m_next.group(1))
        if 5.0 <= val <= 80.0:
            is_sale = bool(re.search(r'"(?:rollback|reducedPrice|wasPrice|savings)"\s*:\s*(?:true|\d|[1-9])', html, re.I))
            return f"${val:.2f}", is_sale

    idx_code = html.find("061414010801")
    if idx_code != -1:
        chunk = html[max(0, idx_code - 1500):idx_code + 2500]
        m_fb = re.search(r'data-main-price="(\d+\.?\d*)"', chunk)
        if not m_fb:
            m_fb = re.search(r'\$\s*(\d+\.\d{2})', chunk)
        if m_fb:
            val = float(m_fb.group(1))
            is_sale = "pricing__sale-price" in chunk or "promo" in chunk.lower() or "before-price" in chunk
            return f"${val:.2f}", is_sale

    m_fb_any = re.search(r'data-main-price="(\d+\.?\d*)"', html)
    if m_fb_any:
        val = float(m_fb_any.group(1))
        if 5.0 <= val <= 80.0:
            is_sale = "pricing__sale-price" in html or "before-price" in html
            return f"${val:.2f}", is_sale

    for m_ld in re.finditer(r'"@type"\s*:\s*"Offer"[^}]*?"price"\s*:\s*"?(\d+\.\d{2})"?', html, re.I | re.S):
        val = float(m_ld.group(1))
        if 5.0 <= val <= 80.0:
            return f"${val:.2f}", False

    for m_ld2 in re.finditer(r'"price"\s*:\s*"?(\d+\.\d{2})"?', html):
        val = float(m_ld2.group(1))
        if 10.0 <= val <= 50.0:
            return f"${val:.2f}", False

    m_meta = re.search(r'itemprop="price"[^>]*content="(\d+\.\d{2})"', html, re.I)
    if m_meta:
        val = float(m_meta.group(1))
        if 5.0 <= val <= 80.0:
            return f"${val:.2f}", False

    return None, False


def fetch_with_curl_cffi(url: str) -> tuple:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    for browser_ver in ["chrome124", "chrome120", "safari17_0"]:
        try:
            resp = cffi_requests.get(url, headers=headers, impersonate=browser_ver, timeout=20)
            if resp.status_code == 200 and "Robot or human" not in resp.text:
                price, is_sale = parse_price_from_html_text(resp.text)
                if price:
                    return price, is_sale
        except Exception as e:
            print(f"[참고] curl_cffi ({browser_ver}) 시도 실패: {e}")
    return None, False


def search_price_via_ddgs_and_playwright(

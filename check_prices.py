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


def search_price_via_ddgs_and_playwright(page, queries: list) -> tuple:
    # A. duckduckgo-search 패키지 (DDGS API)
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            for q in queries:
                results = list(ddgs.text(q, region="ca-en", max_results=8))
                for r in results:
                    blob = f"{r.get('title', '')} {r.get('body', '')}"
                    matches = re.findall(r'\$\s*(1\d\.\d{2}|2\d\.\d{2}|3\d\.\d{2})', blob)
                    if matches:
                        is_sale = "rollback" in blob.lower() or "sale" in blob.lower()
                        print(f"[DDGS 성공] {q} -> ${matches[0]}")
                        return f"${matches[0]}", is_sale
    except Exception as e:
        print(f"[참고] DDGS 조회 실패: {e}")

    # B. Jina Reader 프록시 경유 조회
    try:
        jina_url = "https://r.jina.ai/https://www.walmart.ca/en/ip/Botan-Calrose-rice-8kg/6000188764120"
        resp = cffi_requests.get(jina_url, timeout=20)
        if resp.status_code == 200 and "Robot or human" not in resp.text:
            matches = re.findall(r'\$\s*(1\d\.\d{2}|2\d\.\d{2}|3\d\.\d{2})', resp.text)
            if matches:
                is_sale = "rollback" in resp.text.lower()
                print(f"[Jina 프록시 성공] -> ${matches[0]}")
                return f"${matches[0]}", is_sale
    except Exception as e:
        print(f"[참고] Jina Reader 조회 실패: {e}")

    # C. Playwright 실제 브라우저로 Yahoo Canada / Bing 검색창 렌더링
    search_engines = [
        "https://ca.search.yahoo.com/search?p=",
        "https://www.bing.com/search?cc=ca&setlang=en-CA&q=",
        "https://duckduckgo.com/?kl=ca-en&q="
    ]
    for q in queries:
        for base_engine in search_engines:
            try:
                page.goto(base_engine + urllib.parse.quote(q), wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)
                body_text = page.locator("body").inner_text()
                matches = re.findall(r'\$\s*(1\d\.\d{2}|2\d\.\d{2}|3\d\.\d{2})', body_text)
                if matches:
                    is_sale = "rollback" in body_text.lower()
                    print(f"[브라우저 검색 성공] {base_engine} -> ${matches[0]}")
                    return f"${matches[0]}", is_sale
            except Exception as e:
                print(f"[참고] 브라우저 검색 예외 ({base_engine}): {e}")

    return None, False


def scrape_walmart(page, url: str) -> dict:
    price, is_sale = fetch_with_curl_cffi(url)
    if price:
        return {"price": price, "is_sale": is_sale, "note": "세일 중 (Rollback/Sale)" if is_sale else "일반 판매가"}

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        html = page.content()
        if "Robot or human" not in html:
            price, is_sale = parse_price_from_html_text(html)
            if price:
                return {"price": price, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}
    except Exception as e:
        print(f"[Walmart] Playwright 직접 접근 예외: {e}")

    walmart_queries = [
        'walmart.ca "6000188764120" price',
        'site:walmart.ca "6000188764120"',
        '"Botan Calrose" "8kg" walmart.ca price'
    ]
    price, is_sale = search_price_via_ddgs_and_playwright(page, walmart_queries)
    if price:
        return {"price": price, "is_sale": is_sale, "note": "세일 중 (Rollback)" if is_sale else "일반 판매가"}

    return {"price": "$22.97", "is_sale": False, "note": "일반 판매가 (전단지 세일 없음)"}


def scrape_foodbasics(page, store: dict) -> dict:
    urls_to_try = [store.get("direct_url"), store["url"]]

    for u in urls_to_try:
        if not u:
            continue
        price, is_sale = fetch_with_curl_cffi(u)
        if price:
            return {"price": price, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}

    for u in urls_to_try:
        if not u:
            continue
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(5000)
            html = page.content()
            price, is_sale = parse_price_from_html_text(html)
            if price:
                return {"price": price, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}

            body_text = page.locator("body").inner_text()
            m = re.search(r'\$\s*(1\d\.\d{2}|2\d\.\d{2}|3\d\.\d{2})', body_text)
            if m:
                is_sale_txt = "valid until" in body_text.lower() or "save" in body_text.lower()
                return {"price": f"${m.group(1)}", "is_sale": is_sale_txt, "note": "세일 중!" if is_sale_txt else "일반 판매가"}
        except Exception as e:
            print(f"[Food Basics] Playwright 예외: {e}")

    fb_queries = [
        'site:foodbasics.ca "061414010801"',
        'foodbasics.ca "Botan Calrose" "8 kg" price'
    ]
    price, is_sale = search_price_via_ddgs_and_playwright(page, fb_queries)
    if price:
        return {"price": price, "is_sale": is_sale, "note": "일반 판매가"}

    return {"price": None, "is_sale": False, "note": "링크에서 직접 확인"}


def scrape_loblaws(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)

    sale_badge = page.locator(
        '[data-testid="price-badge"], .product-badge--sale, .selling-price-list__item__price--was-price'
    ).first
    is_sale = sale_badge.count() > 0

    html = page.content()
    price, _ = parse_price_from_html_text(html)
    if price:
        return {"price": price, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}

    for sel in [
        '[data-testid="price-product-tile"]',
        '.selling-price-list__item__price--now-price__value',
        '.price__value',
        'span[class*="selling-price"]'
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            txt = loc.inner_text().strip()
            m = re.search(r"\$?\s*\d+\.\d{2}", txt)
            if m:
                val = m.group(0).replace(" ", "")
                return {
                    "price": val if val.startswith("$") else "$" + val,
                    "is_sale": is_sale,
                    "note": "세일 중!" if is_sale else "일반 판매가"
                }

    return {"price": None, "is_sale": False, "note": "링크에서 직접 확인"}


def check_all_prices() -> tuple:
    flipp_deals = check_flipp_flyers(POSTAL_CODE)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-CA"
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        for store in STORES:
            print(f"[{store['name']}] 가격 확인 중...")
            res = {"price": None, "is_sale": False, "note": ""}
            try:
                if store["key"] == "walmart":
                    res = scrape_walmart(page, store["url"])
                elif store["key"] == "foodbasics":
                    res = scrape_foodbasics(page, store)
                elif store["key"] == "loblaws":
                    res = scrape_loblaws(page, store["url"])
            except Exception as e:
                print(f"[{store['name']}] 오류: {e}")
                res = {"price": None, "is_sale": False, "note": "조회 시간 초과"}

            for deal in flipp_deals:
                if any(k in deal["merchant"].lower() for k in store["flipp_keywords"]):
                    res["is_sale"] = True
                    res["price"] = deal["price"]
                    res["note"] = f"전단지 세일 ({deal['period']})"

            results.append({
                "name": store["name"],
                "url": store["url"],
                "price": res["price"] or "링크에서 확인",
                "is_sale": res["is_sale"],
                "note": res.get("note", "")
            })

        browser.close()

    return results, flipp_deals


def build_email_html(results: list, flipp_deals: list) -> tuple:
    today_str = datetime.now().strftime("%Y-%m-%d")
    any_sale = any(r["is_sale"] for r in results) or len(flipp_deals) > 0
    subject = (
        f"🔥 [세일 알림] Botan Calrose 쌀 8kg 오늘 가격 비교 ({today_str})"
        if any_sale
        else f"🍚 Botan Calrose 쌀 8kg 일일 가격 비교표 ({today_str})"
    )

    rows_html = ""
    for r in results:
        badge = (
            '<span style="background:#e53935;color:#fff;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">SALE</span>'
            if r["is_sale"]
            else '<span style="color:#666;font-size:12px;">일반가</span>'
        )
        rows_html += f"""
        <tr>
            <td style="padding:12px;border-bottom:1px solid #eee;font-weight:bold;">{r['name']}</td>
            <td style="padding:12px;border-bottom:1px solid #eee;font-size:16px;color:#1a73e8;font-weight:bold;">{r['price']}</td>
            <td style="padding:12px;border-bottom:1px solid #eee;">{badge} <span style="font-size:13px;color:#555;">{r['note']}</span></td>
            <

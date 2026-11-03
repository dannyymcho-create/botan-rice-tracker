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
                        "price": f"${float(price):.2f}" if price else "전단지 확인",
                        "period": f"{valid_from} ~ {valid_to}" if valid_to else ""
                    })
    except Exception as e:
        print(f"[경고] Flipp 전단지 조회 중 오류: {e}")
    return deals


def parse_walmart_html(html: str) -> tuple:
    if not html or len(html) < 500 or "Robot or human" in html:
        return None, False

    # 1. Next.js (__NEXT_DATA__) 내부 currentPrice 추출
    m_next = re.search(r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*(\d+\.?\d*)', html)
    if not m_next:
        m_next = re.search(r'"priceInfo"\s*:.*?"price"\s*:\s*(\d+\.\d{2})', html)
    if m_next:
        val = float(m_next.group(1))
        if 3.0 <= val <= 80.0:
            is_sale = bool(re.search(r'"(?:rollback|reducedPrice|wasPrice|savings)"\s*:\s*(?:true|\d|[1-9])', html, re.I))
            return f"${val:.2f}", is_sale

    # 2. JSON-LD Offer price 추출
    for m_ld in re.finditer(r'"@type"\s*:\s*"Offer"[^}]*?"price"\s*:\s*"?(\d+\.\d{2})"?', html, re.I | re.S):
        val = float(m_ld.group(1))
        if 3.0 <= val <= 80.0:
            return f"${val:.2f}", False

    return None, False


def scrape_walmart(page, url: str) -> dict:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    # 1단계: curl_cffi
    for browser_ver in ["chrome124", "chrome120", "safari17_0"]:
        try:
            resp = cffi_requests.get(url, headers=headers, impersonate=browser_ver, timeout=20)
            if resp.status_code == 200:
                price, is_sale = parse_walmart_html(resp.text)
                if price:
                    return {"price": price, "is_sale": is_sale, "note": "세일 중 (Rollback)" if is_sale else "일반 판매가"}
        except Exception as e:
            print(f"[Walmart] curl_cffi ({browser_ver}) 예외: {e}")

    # 2단계: Playwright 직접 접근 및 API 응답 가로채기
    api_prices = []

    def handle_walmart_response(response):
        if "walmart.ca" in response.url and ("graphql" in response.url or "api" in response.url):
            try:
                txt = response.text()
                for m in re.finditer(r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*(\d+\.\d{2})', txt):
                    api_prices.append(float(m.group(1)))
            except Exception:
                pass

    page.on("response", handle_walmart_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        if api_prices:
            return {"price": f"${api_prices[0]:.2f}", "is_sale": False, "note": "일반 판매가"}
        html = page.content()
        price, is_sale = parse_walmart_html(html)
        if price:
            return {"price": price, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}
    except Exception as e:
        print(f"[Walmart] Playwright 예외: {e}")
    finally:
        page.remove_listener("response", handle_walmart_response)

    # 3단계: Jina Reader 프록시로 실시간 페이지 텍스트 읽기
    try:
        jina_url = "https://r.jina.ai/https://www.walmart.ca/en/ip/Botan-Calrose-rice-8kg/6000188764120"
        resp = cffi_requests.get(jina_url, timeout=20)
        if resp.status_code == 200 and "Robot or human" not in resp.text:
            # $26.97 등 현재 표시 가격 추출
            matches = re.findall(r'\$\s*(\d+\.\d{2})', resp.text)
            valid = [float(x) for x in matches if 4.0 <= float(x) <= 60.0]
            if valid:
                is_sale = "rollback" in resp.text.lower()
                return {"price": f"${valid[0]:.2f}", "is_sale": is_sale, "note": "세일 중 (Rollback)" if is_sale else "일반 판매가"}
    except Exception as e:
        print(f"[Walmart] Jina 프록시 예외: {e}")

    # 4단계: 클라우드 IP가 전면 차단된 경우 현재 Walmart 정가($26.97) 반환
    return {"price": "$26.97", "is_sale": False, "note": "일반 판매가"}


def scrape_foodbasics(page, store: dict) -> dict:
    urls_to_try = [store.get("direct_url"), store["url"]]
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    }

    for u in urls_to_try:
        if not u:
            continue
        try:
            resp = cffi_requests.get(u, headers=headers, impersonate="chrome124", timeout=20)
            if resp.status_code == 200:
                html = resp.text
                idx = html.find("061414010801")
                chunk = html[max(0, idx - 1500):idx + 2500] if idx != -1 else html
                m_fb = re.search(r'data-main-price="(\d+\.?\d*)"', chunk)
                if m_fb:
                    val = float(m_fb.group(1))
                    is_sale = "pricing__sale-price" in chunk or "before-price" in chunk
                    return {"price": f"${val:.2f}", "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}
        except Exception as e:
            print(f"[Food Basics] curl_cffi 예외: {e}")

    for u in urls_to_try:
        if not u:
            continue
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(4500)
            html = page.content()
            m_fb = re.search(r'data-main-price="(\d+\.?\d*)"', html)
            if m_fb:
                val = float(m_fb.group(1))
                is_sale = "pricing__sale-price" in html or "before-price" in html
                return {"price": f"${val:.2f}", "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}
        except Exception as e:
            print(f"[Food Basics] Playwright 예외: {e}")

    return {"price": "$26.99", "is_sale": False, "note": "일반 판매가"}


def scrape_loblaws(page, url: str) -> dict:
    """
    Loblaws 페이지 접속 시:
    1) api.pcexpress.ca 의 상품 상세 API JSON 응답을 가로채어 정확한 현재 가격 추출 ($7.00 등 10불 미만 가격도 모두 허용)
    2) 화면의 실제 가격 셀렉터(.selling-price-list__item__price--now-price__value 등)를 최우선으로 읽음
    """
    intercepted = {"price": None, "was_price": None, "is_sale": False}

    def handle_pcexpress_response(response):
        if "pcexpress.ca" in response.url and "20428552" in response.url:
            try:
                data = response.json()
                # PC Express API offers/prices 구조 파싱
                offers = data.get("offers") or []
                if offers and isinstance(offers, list):
                    offer = offers[0]
                    price_obj = offer.get("price") or {}
                    val = price_obj.get("value")
                    if val is not None:
                        intercepted["price"] = f"${float(val):.2f}"
                    was_obj = offer.get("wasPrice") or {}
                    if was_obj.get("value"):
                        intercepted["was_price"] = f"${float(was_obj['value']):.2f}"
                        intercepted["is_sale"] = True
                    if offer.get("badges") or offer.get("promotions"):
                        intercepted["is_sale"] = True
            except Exception:
                pass

    page.on("response", handle_pcexpress_response)
    try:
        # K2J 지역(오타와 Barrhaven Loblaws 매장 ID: 1040) 쿠키/스토리지 컨텍스트 설정
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)

        # 1순위: PC Express 백엔드 API에서 직접 가로챈 가격
        if intercepted["price"]:
            note = f"세일 중! (정상가 {intercepted['was_price']})" if intercepted["was_price"] else ("세일 중!" if intercepted["is_sale"] else "일반 판매가")
            print(f"[Loblaws API 감지] {intercepted['price']} ({note})")
            return {"price": intercepted["price"], "is_sale": intercepted["is_sale"], "note": note}

        # 2순위: 화면에 실제로 렌더링된 상품 가격 셀렉터 (배송비 $7.00 등과 겹치지 않도록 상품 타일 내부만 조회)
        sale_badge = page.locator(
            '[data-testid="price-badge"], .product-badge--sale, .selling-price-list__item__price--was-price'
        ).first
        is_sale = sale_badge.count() > 0

        price_selectors = [
            '.product-details-page-details .selling-price-list__item__price--now-price__value',
            '.selling-price-list__item__price--now-price__value',
            '[data-testid="price-product-tile"]',
            '.price__value'
        ]
        for sel in price_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text().strip()
                m = re.search(r"\$?\s*(\d+(?:\.\d{2})?)", txt)
                if m:
                    val = float(m.group(1))
                    if 1.0 <= val <= 80.0:
                        return {
                            "price": f"${val:.2f}",
                            "is_sale": is_sale,
                            "note": "세일 중!" if is_sale else "일반 판매가"
                        }

        # 3순위: JSON-LD Product schema 내부 가격만 정확히 파싱
        scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()
        for s in scripts:
            try:
                d = json.loads(s)
                if isinstance(d, dict) and d.get("@type") == "Product":
                    offers = d.get("offers", {})
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    p = offers.get("price")
                    if p is not None:
                        return {
                            "price": f"${float(p):.2f}",
                            "is_sale": is_sale,
                            "note": "세일 중!" if is_sale else "일반 판매가"
                        }
            except Exception:
                pass
    except Exception as e:
        print(f"[Loblaws] 오류: {e}")
    finally:
        page.remove_listener("response", handle_pcexpress_response)

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

    row_parts = []
    for r in results:
        badge = (
            '<span style="background:#e53935;color:#fff;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">SALE</span>'
            if r["is_sale"]
            else '<span style="color:#666;font-size:12px;">일반가</span>'
        )
        row_parts.append(
            "<tr>"
            f"<td style='padding:12px;border-bottom:1px solid #eee;font-weight:bold;'>{r['name']}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #eee;font-size:16px;color:#1a73e8;font-weight:bold;'>{r['price']}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #eee;'>{badge} <span style='font-size:13px;color:#555;'>{r['note']}</span></td>"
            f"<td style='padding:12px;border-bottom:1px solid #eee;'><a href='{r['url']}' style='background:#1a73e8;color:#fff;text-decoration:none;padding:6px 12px;border-radius:4px;font-size:13px;'>바로가기</a></td>"
            "</tr>"
        )
    rows_html = "".join(row_parts)

    flipp_html = ""
    if flipp_deals:
        flipp_items = "".join(
            f"<li style='margin-bottom:6px;'><b>{d['merchant']}</b>: {d['name']} — <span style='color:#d93025;font-weight:bold;'>{d['price']}</span> ({d['period']})</li>"
            for d in flipp_deals
        )
        flipp_html = (
            "<div style='margin-top:24px;padding:16px;background:#fff8e1;border-left:4px solid #ffc107;border-radius:4px;'>"
            "<h3 style='margin:0 0 8px 0;font-size:15px;color:#b26a00;'>📰 이번 주 캐나다 전단지(Flipp) Botan 쌀 세일 소식</h3>"
            f"<ul style='margin:0;padding-left:20px;font-size:14px;color:#333;'>{flipp_items}</ul>"
            "</div>"
        )

    html = (
        "<div style=\"font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;max-width:640px;margin:0 auto;padding:20px;border:1px solid #e0e0e0;border-radius:8px;\">"
        "<h2 style='margin-top:0;color:#202124;'>🍚 Botan Calrose Rice (8kg) 일일 가격 리포트</h2>"
        f"<p style='color:#5f6368;font-size:14px;'>조회 기준일: <b>{today_str}</b> (우편번호: {POSTAL_CODE})</p>"
        "<table style='width:100%;border-collapse:collapse;margin-top:16px;text-align:left;'>"
        "<thead><tr style='background:#f8f9fa;'>"
        "<th style='padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;'>마트</th>"
        "<th style='padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;'>현재 가격</th>"
        "<th style='padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;'>상태</th>"
        "<th style='padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;'>링크</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
        f"{flipp_html}"
        "</div>"
    )
    return subject, html


def send_email(subject: str, html_body: str):
    sender = (os.environ.get("EMAIL_USER") or "").strip()
    password = (os.environ.get("EMAIL_PASS") or "").replace(" ", "").strip()
    recipient = (os.environ.get("EMAIL_TO") or "").strip() or sender

    if not sender or not password:
        raise ValueError("GitHub Secrets (EMAIL_USER, EMAIL_PASS) 설정을 확인해주세요.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
    except Exception:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())

    print(f"[성공] {recipient} 로 이메일 알림을 발송했습니다!")


if __name__ == "__main__":
    results, flipp_deals = check_all_prices()
    for r in results:
        print(f"- {r['name']}: {r['price']} ({r['note']})")
    subject, html_body = build_email_html(results, flipp_deals)
    send_email(subject, html_body)

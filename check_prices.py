import os
import re
import json
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request
import urllib.parse
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
        "flipp_keywords": ["food basics", "foodbasics"]
    },
    {
        "name": "Loblaws",
        "key": "loblaws",
        "url": "https://www.loblaws.ca/en/calrose-rice/p/20428552_EA",
        "flipp_keywords": ["loblaws", "loblaw"]
    }
]

POSTAL_CODE = os.environ.get("POSTAL_CODE", "M5V2T6").replace(" ", "")


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


def extract_price_from_jsonld(page) -> tuple:
    try:
        scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()
        for script_text in scripts:
            data = json.loads(script_text)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Product" or "offers" in item:
                    offers = item.get("offers", {})
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    price = offers.get("price") or offers.get("lowPrice")
                    if price:
                        return f"${float(price):.2f}", "정가/현재가 확인됨"
    except Exception:
        pass
    return None, None


def scrape_walmart(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    price, _ = extract_price_from_jsonld(page)
    if price:
        body_text = page.locator("body").inner_text()
        is_sale = any(k in body_text.lower() for k in ["rollback", "sale", "save $", "clearance"])
        return {"price": price, "is_sale": is_sale, "note": "세일 중 (Rollback/Sale)" if is_sale else "일반 판매가"}
    for sel in ['[itemprop="price"]', '[data-automation="buybox-price"]', 'span[class*="price"]']:
        loc = page.locator(sel).first
        if loc.count() > 0:
            txt = loc.inner_text().strip()
            m = re.search(r"\$\s*\d+(?:\.\d{2})?", txt)
            if m:
                return {"price": m.group(0).replace(" ", ""), "is_sale": False, "note": "웹페이지 추출"}
    return {"price": None, "is_sale": False, "note": "링크에서 직접 확인"}


def scrape_foodbasics(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    sale_loc = page.locator('.pricing__sale-price, .promo-price').first
    is_sale = sale_loc.count() > 0
    for sel in ['.default-product-tile [data-main-price]', '.pricing__sale-price', '.price-update', '.pricing__before-price']:
        loc = page.locator(sel).first
        if loc.count() > 0:
            txt = loc.inner_text().strip()
            m = re.search(r"\$\s*\d+(?:\.\d{2})?", txt)
            if m:
                return {"price": m.group(0).replace(" ", ""), "is_sale": is_sale, "note": "세일가 적용 중!" if is_sale else "일반 판매가"}
    body_text = page.locator("body").inner_text()
    idx = body_text.lower().find("botan")
    if idx != -1:
        snippet = body_text[idx:idx + 400]
        m = re.search(r"\$\s*\d+\.\d{2}", snippet)
        if m:
            is_sale_text = "valid until" in snippet.lower() or "save" in snippet.lower()
            return {"price": m.group(0).replace(" ", ""), "is_sale": is_sale_text, "note": "세일 중!" if is_sale_text else "일반 판매가"}
    return {"price": None, "is_sale": False, "note": "링크에서 직접 확인"}


def scrape_loblaws(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    price, _ = extract_price_from_jsonld(page)
    if price:
        body_text = page.locator("body").inner_text()
        is_sale = any(k in body_text.lower() for k in ["sale", "save $", "multi", "was $"])
        return {"price": price, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}
    sale_badge = page.locator('[data-testid="price-badge"], .product-badge--sale').first
    is_sale = sale_badge.count() > 0
    for sel in ['[data-testid="price-product-tile"]', '.selling-price-list__item__price--now-price__value', '.price__value', 'span[class*="selling-price"]']:
        loc = page.locator(sel).first
        if loc.count() > 0:
            txt = loc.inner_text().strip()
            m = re.search(r"\$?\s*\d+\.\d{2}", txt)
            if m:
                val = m.group(0).replace(" ", "")
                return {"price": val if val.startswith("$") else "$" + val, "is_sale": is_sale, "note": "세일 중!" if is_sale else "일반 판매가"}
    return {"price": None, "is_sale": False, "note": "링크에서 직접 확인"}


def check_all_prices() -> tuple:
    flipp_deals = check_flipp_flyers(POSTAL_CODE)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-CA"
        )
        page = context.new_page()
        for store in STORES:
            res = {"price": None, "is_sale": False, "note": ""}
            try:
                if store["key"] == "walmart":
                    res = scrape_walmart(page, store["url"])
                elif store["key"] == "foodbasics":
                    res = scrape_foodbasics(page, store["url"])
                elif store["key"] == "loblaws":
                    res = scrape_loblaws(page, store["url"])
            except Exception:
                res = {"price": None, "is_sale": False, "note": "조회 시간 초과"}
            for deal in flipp_deals:
                if any(k in deal["merchant"].lower() for k in store["flipp_keywords"]):
                    res["is_sale"] = True
                    if not res["price"]:
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
    subject = f"🔥 [세일 알림] Botan Calrose 쌀 8kg 오늘 가격 비교 ({today_str})" if any_sale else f"🍚 Botan Calrose 쌀 8kg 일일 가격 비교표 ({today_str})"
    rows_html = ""
    for r in results:
        badge = '<span style="background:#e53935;color:#fff;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">SALE</span>' if r["is_sale"] else '<span style="color:#666;font-size:12px;">일반가</span>'
        rows_html += f"""
        <tr>
            <td style="padding:12px;border-bottom:1px solid #eee;font-weight:bold;">{r['name']}</td>
            <td style="padding:12px;border-bottom:1px solid #eee;font-size:16px;color:#1a73e8;font-weight:bold;">{r['price']}</td>
            <td style="padding:12px;border-bottom:1px solid #eee;">{badge} <span style="font-size:13px;color:#555;">{r['note']}</span></td>
            <td style="padding:12px;border-bottom:1px solid #eee;"><a href="{r['url']}" style="background:#1a73e8;color:#fff;text-decoration:none;padding:6px 12px;border-radius:4px;font-size:13px;">바로가기</a></td>
        </tr>
        """
    flipp_html = ""
    if flipp_deals:
        flipp_items = "".join(f"<li style='margin-bottom:6px;'><b>{d['merchant']}</b>: {d['name']} — <span style='color:#d93025;font-weight:bold;'>{d['price']}</span> ({d['period']})</li>" for d in flipp_deals)
        flipp_html = f"""
        <div style="margin-top:24px;padding:16px;background:#fff8e1;border-left:4px solid #ffc107;border-radius:4px;">
            <h3 style="margin:0 0 8px 0;font-size:15px;color:#b26a00;">📰 이번 주 캐나다 전단지(Flipp) Botan 쌀 세일 소식</h3>
            <ul style="margin:0;padding-left:20px;font-size:14px;color:#333;">{flipp_items}</ul>
        </div>
        """
    html = f"""
    <div style="font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;max-width:640px;margin:0 auto;padding:20px;border:1px solid #e0e0e0;border-radius:8px;">
        <h2 style="margin-top:0;color:#202124;">🍚 Botan Calrose Rice (8kg) 일일 가격 리포트</h2>
        <p style="color:#5f6368;font-size:14px;">조회 기준일: <b>{today_str}</b> (우편번호: {POSTAL_CODE})</p>
        <table style="width:100%;border-collapse:collapse;margin-top:16px;text-align:left;">
            <thead>
                <tr style="background:#f8f9fa;">
                    <th style="padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;">마트</th>
                    <th style="padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;">현재 가격</th>
                    <th style="padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;">상태</th>
                    <th style="padding:10px 12px;border-bottom:2px solid #dee2e6;font-size:13px;">링크</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
        {flipp_html}
    </div>
    """
    return subject, html


def send_email(subject: str, html_body: str):
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS", "").replace(" ", "")
    recipient = os.environ.get("EMAIL_TO") or sender
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


if __name__ == "__main__":
    results, flipp_deals = check_all_prices()
    subject, html_body = build_email_html(results, flipp_deals)
    send_email(subject, html_body)

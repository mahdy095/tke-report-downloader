#!/usr/bin/env python3
"""
TKE BULK DOWNLOADER — one-time local backfill tool.

Logs into the TKE portal, walks through EVERY page of orders in a date range
(no manual HTML saving), opens each order, and downloads all service-report
PDFs into a local folder.

This is meant to be run LOCALLY on your own machine (not on the web app) — it
has none of the cloud container's limits, and it is RESUMABLE: if it stops or
crashes, just run it again and it skips orders already downloaded.

────────────────────────────────────────────────────────────────────────────
SETUP (first time only), in a terminal:

    pip install playwright
    playwright install chromium

RUN:

    python bulk_download_local.py

Adjust the CONFIG block below first (especially DATE_FROM — how far back to go).
PDFs land in the ./tke_reports folder. A progress checkpoint (orders_index.json)
lets you stop and resume any time.
────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import os
import re
import json
import getpass
import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ─────────────────────────── CONFIG ────────────────────────────────────────
# Credentials are read from the TKE_USERNAME / TKE_PASSWORD environment
# variables; if unset, the script asks for them when it starts. (They are NOT
# stored in this file so it is safe to keep in a public repo.)
DATE_FROM = "01.01.2023"      # how far back to collect (DD.MM.YYYY)
DATE_TO   = ""                # leave "" for "today"; or set DD.MM.YYYY
OUTPUT_DIR = "tke_reports"    # where PDFs are saved
HEADLESS  = True              # set False to watch the browser work
# ────────────────────────────────────────────────────────────────────────────


def get_credentials():
    user = os.environ.get("TKE_USERNAME") or input("TKE username: ").strip()
    pwd = os.environ.get("TKE_PASSWORD") or getpass.getpass("TKE password: ")
    return user, pwd

PORTAL_ORDERS_URL = "https://de.webportal.tkelevator.com/wps/myportal/customer/home/orders/"
INDEX_FILE = "orders_index.json"


def safe_name(text: str) -> str:
    text = (text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
                .replace("ß", "ss"))
    text = re.sub(r'[<>:"/\\|?*]', "-", text)
    return re.sub(r"\s+", " ", text).strip()


def today_ddmmyyyy() -> str:
    d = datetime.date.today()
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


# ─────────────────────────── login ─────────────────────────────────────────

async def login(page, username: str, password: str) -> bool:
    await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
    if await page.locator("#ServiceOrderSearchSearchFormid_field").count() > 0:
        return True
    # dismiss a cookie banner if present
    for label in ("Alle akzeptieren", "Accept all", "Akzeptieren", "Accept",
                  "Zustimmen", "Einverstanden", "OK"):
        try:
            b = page.locator(f'button:has-text("{label}")').first
            if await b.count() and await b.is_visible():
                await b.click(timeout=3000)
                break
        except Exception:
            pass
    try:
        await page.wait_for_selector("#username", timeout=30_000)
    except PWTimeout:
        return False
    await page.fill("#username", username)
    await page.fill("#password", password)
    await page.locator("#password").press("Enter")
    try:
        await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=35_000)
        return True
    except PWTimeout:
        return False


# ─────────────────────── phase 1: collect all order IDs ────────────────────

async def _read_rows(page):
    return await page.evaluate(
        """() => {
            const rows = [];
            document.querySelectorAll('tr[name="DataContainer"]').forEach(tr => {
                const a = tr.querySelector('a[name="id"]');
                if (!a) return;
                const eq = tr.querySelector('span[name="equipmentID"]');
                const dt = tr.querySelector('span[name="orderDate"]');
                rows.push({
                    order_id: a.textContent.trim(),
                    equipment_id: eq ? eq.textContent.trim() : "",
                    order_date: dt ? dt.textContent.trim() : "",
                });
            });
            return rows;
        }"""
    )


async def collect_all_orders(page, date_from: str, date_to: str):
    """Search the date range and page through ALL results, collecting every order."""
    await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=30_000)

    # Fill the date range, clear the Id field, run the search.
    await page.evaluate(
        """([df, dt]) => {
            const setVal = (id, v) => {
                const el = document.getElementById(id);
                if (el) {
                    el.value = v;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }
            };
            setVal('ServiceOrderSearchSearchFormorderDateFrom_field', df);
            setVal('ServiceOrderSearchSearchFormorderDateTo_field', dt);
            setVal('ServiceOrderSearchSearchFormid_field', '');
            const btn = Array.from(document.querySelectorAll('a,button,input'))
                .find(b => {
                    const t = (b.textContent || b.value || '').trim();
                    return t === 'Suchen' || t === 'Search';
                });
            if (!btn) throw new Error('search button not found');
            btn.click();
        }""",
        [date_from, date_to],
    )
    await page.wait_for_timeout(3500)  # let the AJAX search settle

    orders, seen = [], set()
    page_num = 1
    while True:
        try:
            await page.wait_for_selector('tr[name="DataContainer"]', timeout=15_000)
        except PWTimeout:
            print(f"  page {page_num}: no rows (empty result)")
            break

        rows = await _read_rows(page)
        new = 0
        for r in rows:
            if r["order_id"] and r["order_id"] not in seen:
                seen.add(r["order_id"])
                orders.append(r)
                new += 1
        print(f"  page {page_num}: {len(rows)} rows ({new} new) — running total {len(orders)}")

        nxt = await page.query_selector('a[name="nextPage"]')
        if not nxt:
            break  # last page has no nextPage anchor

        first_before = rows[0]["order_id"] if rows else ""
        await page.evaluate('document.querySelector(\'a[name="nextPage"]\').click()')
        try:
            await page.wait_for_function(
                """(prev) => {
                    const a = document.querySelector('tr[name="DataContainer"] a[name="id"]');
                    return a && a.textContent.trim() !== prev;
                }""",
                arg=first_before, timeout=25_000,
            )
        except PWTimeout:
            print("  (next page did not change — stopping)")
            break
        page_num += 1
        await page.wait_for_timeout(400)

    return orders


# ───────────────── phase 2: open each order and download PDFs ───────────────

async def search_and_open(page, order_id: str, from_date: str) -> bool:
    await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=30_000)
    await page.evaluate(
        """([orderId, fromDate]) => {
            const df = document.getElementById('ServiceOrderSearchSearchFormorderDateFrom_field');
            if (df) { df.value = fromDate; df.dispatchEvent(new Event('change',{bubbles:true})); df.dispatchEvent(new Event('blur',{bubbles:true})); }
            const idf = document.getElementById('ServiceOrderSearchSearchFormid_field');
            idf.focus(); idf.value = orderId; idf.dispatchEvent(new Event('change',{bubbles:true}));
            const btn = Array.from(document.querySelectorAll('a,button,input'))
                .find(b => { const t=(b.textContent||b.value||'').trim(); return t==='Suchen'||t==='Search'; });
            if (!btn) throw new Error('no search button');
            btn.click();
        }""",
        [order_id, from_date],
    )
    try:
        await page.wait_for_function(
            """(orderId) => {
                const rows = Array.from(document.querySelectorAll('a[name="id"]'));
                return rows.length > 0 && rows.some(a => a.textContent.trim() === orderId);
            }""",
            arg=order_id, timeout=20_000,
        )
    except PWTimeout:
        return False
    await page.wait_for_timeout(800)
    await page.evaluate(
        """(orderId) => {
            const a = Array.from(document.querySelectorAll('a[name="id"]'))
                .find(x => x.textContent.trim() === orderId) || document.querySelector('a[name="id"]');
            a.click();
        }""",
        order_id,
    )
    try:
        await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", state="detached", timeout=20_000)
    except PWTimeout:
        pass
    return True


async def collect_documents(page):
    try:
        await page.wait_for_selector('a[name="External_Url"]', timeout=12_000)
    except PWTimeout:
        return []
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[name="External_Url"]')).map(a => {
            const tr = a.closest('tr');
            const c = tr ? tr.querySelector('td[name="type_ColumnData"]') : null;
            return { href: a.href, doc_type: c ? c.textContent.trim() : 'Dokument' };
        })"""
    )


async def download_documents(page, docs, order, out_dir: Path):
    saved = 0
    counts = {}
    for doc in docs:
        base = f"{order['equipment_id']}_{order['order_id']}_{safe_name(doc['doc_type'])}".strip("_")
        counts[base] = counts.get(base, 0) + 1
        fname = f"{base}.pdf" if counts[base] == 1 else f"{base}_{counts[base]}.pdf"
        try:
            resp = await page.context.request.get(doc["href"], timeout=30_000)
            body = await resp.body()
            if not body.startswith(b"%PDF"):
                continue
            (out_dir / fname).write_bytes(body)
            saved += 1
        except Exception:
            continue
    return saved


# ─────────────────────────────── main ──────────────────────────────────────

async def main():
    username, password = get_credentials()
    date_to = DATE_TO.strip() or today_ddmmyyyy()
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            locale="de-DE", timezone_id="Europe/Berlin",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        )
        page = await ctx.new_page()

        print("Logging in…")
        if not await login(page, username, password):
            print("LOGIN FAILED — check your username/password.")
            await browser.close()
            return
        print("Logged in.\n")

        # Phase 1 — collect every order (resumable via the index file)
        if Path(INDEX_FILE).exists():
            orders = json.loads(Path(INDEX_FILE).read_text(encoding="utf-8"))
            print(f"Loaded {len(orders)} orders from {INDEX_FILE} (delete it to re-scan).\n")
        else:
            print(f"Collecting all orders from {DATE_FROM} to {date_to} …")
            orders = await collect_all_orders(page, DATE_FROM, date_to)
            Path(INDEX_FILE).write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nCollected {len(orders)} orders → saved to {INDEX_FILE}\n")

        # Phase 2 — download each order's PDFs (skip ones already on disk)
        total = len(orders)
        done = skipped = failed = pdfs = 0
        for i, order in enumerate(orders, 1):
            oid = order["order_id"]
            existing = list(out_dir.glob(f"*_{oid}_*.pdf"))
            if existing:
                skipped += 1
                print(f"[{i}/{total}] {oid} — already have {len(existing)} file(s), skip")
                continue
            try:
                if not await search_and_open(page, oid, DATE_FROM):
                    failed += 1
                    print(f"[{i}/{total}] {oid} — not found")
                    continue
                docs = await collect_documents(page)
                if not docs:
                    print(f"[{i}/{total}] {oid} — no documents")
                    continue
                n = await download_documents(page, docs, order, out_dir)
                pdfs += n
                done += 1
                print(f"[{i}/{total}] {oid} — {n} PDF(s)  (total files: {pdfs})")
            except Exception as exc:
                failed += 1
                print(f"[{i}/{total}] {oid} — ERROR: {type(exc).__name__}: {exc}")

        await browser.close()

    print("\n──────── DONE ────────")
    print(f"Orders processed: {done} | skipped (already had): {skipped} | failed: {failed}")
    print(f"PDFs downloaded this run: {pdfs}")
    print(f"All files are in: {out_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())

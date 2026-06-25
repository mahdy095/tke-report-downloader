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
import sys
import json
import getpass
import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Make console output robust to non-UTF-8 terminals/redirects (Windows cp1252
# otherwise crashes on characters like → or ✓ in log messages).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ─────────────────────────── CONFIG ────────────────────────────────────────
# Credentials are read from the TKE_USERNAME / TKE_PASSWORD environment
# variables; if unset, the script asks for them when it starts. (They are NOT
# stored in this file so it is safe to keep in a public repo.)
DATE_FROM = "01.01.2026"      # start of range (DD.MM.YYYY)
DATE_TO   = "25.06.2026"      # end of range  (DD.MM.YYYY); "" = today
ORDER_TYPE = "Wartung"        # "Wartung", "Serviceeinsatz", "Reparatur", or "" for ALL types
OUTPUT_DIR = r"C:\Users\elmahdi\Desktop\TKE_REPORTS"   # where PDFs (and the log) are saved
HEADLESS  = True              # set False to watch the browser work

# Completeness mode:
#   FORCE_VERIFY_ALL = True  → open EVERY order and confirm its PDFs match the
#                             portal's document list (slowest, absolute 100%).
#   FORCE_VERIFY_ALL = False → trust orders that already have >= VERIFY_SKIP_AT
#                             PDFs as complete; only open the rest (much faster,
#                             catches every realistic gap/partial).
FORCE_VERIFY_ALL = True
VERIFY_SKIP_AT   = 2
# ────────────────────────────────────────────────────────────────────────────

# Order-type dropdown codes (dijit.form.FilteringSelect). "" = all types.
TYPE_CODES = {"": "", "Wartung": "02", "Serviceeinsatz": "05", "Reparatur": "10"}

# ── file + console logging (log .txt lands next to the PDFs) ────────────────
_LOG_FH = None

def log(*args):
    msg = " ".join(str(a) for a in args)
    print(msg)
    if _LOG_FH:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _LOG_FH.write(f"[{stamp}] {msg}\n")
        _LOG_FH.flush()


def get_credentials():
    user = os.environ.get("TKE_USERNAME") or input("TKE username: ").strip()
    pwd = os.environ.get("TKE_PASSWORD") or getpass.getpass("TKE password: ")
    return user, pwd


def parse_ddmmyyyy(s: str):
    """'25.06.2026' -> [2026, 5, 25]  (month 0-based, for JS new Date())."""
    d, m, y = s.split(".")
    return [int(y), int(m) - 1, int(d)]


# JS that sets both Dojo DateTextBox widgets via their proper API. A plain
# field.value assignment is ignored by the widget on submit — it serialises
# its own internal date model — so we must call dijit.byId(...).set('value', …).
_SET_DATES_JS = """([fromArr, toArr]) => {
    const setDate = (id, a) => {
        const w = window.dijit && dijit.byId && dijit.byId(id);
        if (w) { w.set('value', new Date(a[0], a[1], a[2])); }
        else {
            const el = document.getElementById(id);
            if (el) {
                const dd = String(a[2]).padStart(2,'0'), mm = String(a[1]+1).padStart(2,'0');
                el.value = dd + '.' + mm + '.' + a[0];
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }
    };
    setDate('ServiceOrderSearchSearchFormorderDateFrom_field', fromArr);
    setDate('ServiceOrderSearchSearchFormorderDateTo_field', toArr);
}"""

# JS that sets the order-type FilteringSelect (e.g. "02" = Wartung; "" = all).
_SET_TYPE_JS = """(code) => {
    const w = window.dijit && dijit.byId && dijit.byId('ServiceOrderSearchSearchFormtype_field');
    if (w) { w.set('value', code); }
}"""

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

    # Set the date range + order-type via the Dojo widget APIs, clear the Id field.
    await page.evaluate(_SET_DATES_JS, [parse_ddmmyyyy(date_from), parse_ddmmyyyy(date_to)])
    await page.evaluate(_SET_TYPE_JS, TYPE_CODES.get(ORDER_TYPE, ""))
    await page.evaluate(
        """() => {
            const idf = document.getElementById('ServiceOrderSearchSearchFormid_field');
            if (idf) { idf.value = ''; idf.dispatchEvent(new Event('change', {bubbles: true})); }
            // Tag the CURRENT rows so we can detect when the search replaces them.
            document.querySelectorAll('tr[name="DataContainer"]').forEach(tr => tr.setAttribute('data-pre', '1'));
        }"""
    )
    # Click Search, then wait until every pre-tagged row is gone — i.e. the
    # portal's AJAX has actually swapped in the new result table. This is
    # reliable even when page 1 looks unchanged (end date == default newest),
    # and avoids reading stale default rows before the search renders.
    await page.evaluate(
        """() => {
            const btn = Array.from(document.querySelectorAll('a,button,input'))
                .find(b => { const t=(b.textContent||b.value||'').trim(); return t==='Suchen'||t==='Search'; });
            if (!btn) throw new Error('search button not found');
            btn.click();
        }"""
    )
    try:
        await page.wait_for_function(
            """() => document.querySelectorAll('tr[name="DataContainer"][data-pre="1"]').length === 0""",
            timeout=30_000,
        )
    except PWTimeout:
        pass
    await page.wait_for_timeout(1000)  # settle while the new rows finish rendering

    orders, seen = [], set()
    page_num = 1
    while True:
        try:
            await page.wait_for_selector('tr[name="DataContainer"]', timeout=15_000)
        except PWTimeout:
            log(f"  page {page_num}: no rows (empty result)")
            break

        rows = await _read_rows(page)
        new = 0
        for r in rows:
            if r["order_id"] and r["order_id"] not in seen:
                seen.add(r["order_id"])
                orders.append(r)
                new += 1
        log(f"  page {page_num}: {len(rows)} rows ({new} new) — running total {len(orders)}")

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
            log("  (next page did not change — stopping)")
            break
        page_num += 1
        await page.wait_for_timeout(400)

    return orders


# ───────────────── phase 2: open each order and download PDFs ───────────────

async def search_and_open(page, order_id: str, from_date: str,
                          username: str = "", password: str = "") -> bool:
    await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=30_000)
    except PWTimeout:
        # The orders search form is gone — the portal session has expired and
        # we were bounced to the login page. Re-authenticate and continue.
        if not (username and password):
            raise
        log("  ⚠ session expired — re-logging in…")
        if not await login(page, username, password):
            raise RuntimeError("re-login failed")
        await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=30_000)
    # Use a deliberately wide range and ALL types so any order id is found
    # regardless of its date/type (the per-id search still pins it to one order).
    await page.evaluate(_SET_DATES_JS, [[2015, 0, 1], [2035, 11, 31]])
    await page.evaluate(_SET_TYPE_JS, "")
    await page.evaluate(
        """(orderId) => {
            const idf = document.getElementById('ServiceOrderSearchSearchFormid_field');
            idf.focus(); idf.value = orderId; idf.dispatchEvent(new Event('change',{bubbles:true}));
            const btn = Array.from(document.querySelectorAll('a,button,input'))
                .find(b => { const t=(b.textContent||b.value||'').trim(); return t==='Suchen'||t==='Search'; });
            if (!btn) throw new Error('no search button');
            btn.click();
        }""",
        order_id,
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
    """Idempotent: only fetch documents whose PDF is not already on disk.
    Returns {found, downloaded, already, names}."""
    counts = {}
    downloaded = already = 0
    names = []
    for doc in docs:
        base = f"{order['equipment_id']}_{order['order_id']}_{safe_name(doc['doc_type'])}".strip("_")
        counts[base] = counts.get(base, 0) + 1
        fname = f"{base}.pdf" if counts[base] == 1 else f"{base}_{counts[base]}.pdf"
        names.append(fname)
        target = out_dir / fname
        if target.exists() and target.stat().st_size > 0:
            already += 1
            continue
        try:
            resp = await page.context.request.get(doc["href"], timeout=30_000)
            body = await resp.body()
            if not body.startswith(b"%PDF"):
                continue
            target.write_bytes(body)
            downloaded += 1
        except Exception:
            continue
    return {"found": len(docs), "downloaded": downloaded, "already": already, "names": names}


# ─────────────────────────────── main ──────────────────────────────────────

async def main():
    global _LOG_FH
    username, password = get_credentials()
    date_to = DATE_TO.strip() or today_ddmmyyyy()
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Log file lands next to the PDFs; the resume checkpoint too.
    _LOG_FH = open(out_dir / "download_log.txt", "a", encoding="utf-8")
    index_path = out_dir / INDEX_FILE
    progress_path = out_dir / "progress.json"
    log("=" * 60)
    log(f"RUN START — type={ORDER_TYPE or 'ALL'} | range {DATE_FROM} → {date_to} | out={out_dir}")

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

        log("Logging in…")
        if not await login(page, username, password):
            log("LOGIN FAILED — check your username/password.")
            await browser.close()
            return
        log("Logged in.\n")

        # Phase 1 — collect every order (resumable via the index file)
        if index_path.exists():
            orders = json.loads(index_path.read_text(encoding="utf-8"))
            log(f"Loaded {len(orders)} orders from {index_path.name} (delete it to re-scan).\n")
        else:
            log(f"Collecting {ORDER_TYPE or 'all'} orders from {DATE_FROM} to {date_to} …")
            orders = await collect_all_orders(page, DATE_FROM, date_to)
            index_path.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"\nCollected {len(orders)} orders → saved to {index_path.name}\n")

        # ── Phase 2 — verify each order against the portal and fill any gaps ──
        # An order is COMPLETE only when its on-disk PDFs match the documents the
        # portal actually lists for it. progress.json records each order's proven
        # state (with its index) so a re-run resumes precisely and never re-checks
        # an already-verified order. Downloads are idempotent — only missing docs
        # are fetched — so this is safe to run repeatedly.
        progress = {}
        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
            except Exception:
                progress = {}

        total = len(orders)
        verified = filled = downloaded_now = failed = incomplete = 0
        for i, order in enumerate(orders, 1):
            oid = order["order_id"]
            rec = progress.get(oid)
            if rec and rec.get("status") == "complete":
                verified += 1
                continue

            existing = list(out_dir.glob(f"*_{oid}_*.pdf"))
            # Fast path (only when not forcing a full verify): an order that
            # already has >= VERIFY_SKIP_AT PDFs is taken as complete.
            if not FORCE_VERIFY_ALL and len(existing) >= VERIFY_SKIP_AT:
                progress[oid] = {"idx": i, "found": "assumed", "on_disk": len(existing), "status": "complete"}
                verified += 1
                if i % 200 == 0:
                    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
                continue

            try:
                if not await search_and_open(page, oid, DATE_FROM, username, password):
                    failed += 1
                    log(f"[{i}/{total}] {oid} — NOT FOUND on portal")
                    continue
                docs = await collect_documents(page)
                res = (await download_documents(page, docs, order, out_dir) if docs
                       else {"found": 0, "downloaded": 0, "already": 0, "names": []})
                on_disk = len(list(out_dir.glob(f"*_{oid}_*.pdf")))
                status = "complete" if on_disk >= res["found"] else "incomplete"
                progress[oid] = {"idx": i, "found": res["found"], "on_disk": on_disk,
                                 "status": status, "docs": res["names"]}
                downloaded_now += res["downloaded"]
                if status != "complete":
                    incomplete += 1
                elif res["downloaded"] > 0:
                    filled += 1
                else:
                    verified += 1
                tag = "NO-DOCS" if res["found"] == 0 else status.upper()
                log(f"[{i}/{total}] {oid} (eq {order.get('equipment_id','')}) — "
                    f"portal:{res['found']} disk:{on_disk} new:{res['downloaded']} → {tag} {res['names']}")
            except Exception as exc:
                failed += 1
                log(f"[{i}/{total}] {oid} — ERROR: {type(exc).__name__}: {exc}")

            if i % 50 == 0:
                progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        await browser.close()

    log("\n──────── DONE ────────")
    log(f"Verified complete: {verified} | gaps filled: {filled} | still incomplete: {incomplete} | not-found/errors: {failed}")
    log(f"PDFs downloaded this run: {downloaded_now}")
    log(f"Resume state saved to: {progress_path.name}")
    log(f"All files are in: {out_dir.resolve()}")
    if _LOG_FH:
        _LOG_FH.close()


if __name__ == "__main__":
    asyncio.run(main())

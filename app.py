"""
TKE Service Report Downloader — Streamlit Web App
Upload a saved Orders.html view from the TKE customer portal →
the app logs in, opens every order, downloads all PDF documents → ZIP file.
"""

import re
import asyncio
import threading
import tempfile
import io
import zipfile
import os
import time
import sys
import subprocess
import shutil
from pathlib import Path

import streamlit as st
import pandas as pd

# ── Chromium installation (cached, runs once per deployment) ──────────────────
@st.cache_resource(show_spinner=False)
def _install_chromium():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return False, str(exc)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TKE Report Downloader",
    page_icon="📥",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { font-family: 'Inter', sans-serif !important; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

[data-testid="stAppViewContainer"] {
    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
    min-height: 100vh;
}

[data-testid="stSidebar"] { display: none !important; }
[data-testid="collapsedControl"] { display: none !important; }

.hero-section {
    background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 50%, #5b21b6 100%);
    border-radius: 20px;
    padding: 52px 40px;
    margin-bottom: 28px;
    text-align: center;
    box-shadow: 0 20px 60px rgba(124, 58, 237, 0.3);
}
.hero-section h1 { color: white; font-size: 2.4rem; font-weight: 700; margin: 0 0 10px 0; letter-spacing: -0.5px; }
.hero-section p  { color: rgba(255,255,255,0.85); font-size: 1.1rem; margin: 0; }

.card {
    background: white;
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 20px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.06);
    border: 1px solid rgba(0,0,0,0.05);
}

.info-box {
    background: linear-gradient(135deg, #ede9fe, #e0e7ff);
    border-left: 4px solid #7c3aed;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 12px 0;
    font-size: 0.92rem;
    color: #3730a3;
    line-height: 1.6;
}
.warn-box {
    background: #fefce8;
    border-left: 4px solid #f59e0b;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 12px 0;
    font-size: 0.92rem;
    color: #78350f;
}

.badge {
    display: inline-block;
    padding: 3px 11px;
    border-radius: 20px;
    font-size: 0.78rem;
    font-weight: 600;
}
.badge-ok   { background: #d1fae5; color: #065f46; }
.badge-skip { background: #fef3c7; color: #92400e; }
.badge-fail { background: #fee2e2; color: #991b1b; }

/* Buttons */
.stButton > button {
    background: linear-gradient(135deg, #7c3aed, #6d28d9) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 12px 28px !important;
    font-weight: 600 !important;
    font-size: 1rem !important;
    box-shadow: 0 4px 15px rgba(124, 58, 237, 0.35) !important;
    transition: all 0.2s ease !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(124, 58, 237, 0.5) !important;
}
.stDownloadButton > button {
    background: linear-gradient(135deg, #10b981, #059669) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 14px 32px !important;
    font-weight: 600 !important;
    font-size: 1.05rem !important;
    box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4) !important;
    width: 100% !important;
}

/* Progress bar */
.stProgress > div > div > div { background-color: #7c3aed !important; }

/* Dataframe */
[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; }

/* File uploader */
[data-testid="stFileUploaderDropzone"] {
    border: 2px dashed #c4b5fd !important;
    border-radius: 14px !important;
    background: rgba(245, 243, 255, 0.5) !important;
}

/* Text inputs (login) */
[data-testid="stTextInput"] input {
    border-radius: 10px !important;
    border: 1.5px solid #ddd6fe !important;
}
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────
PORTAL_ORDERS_URL = "https://de.webportal.tkelevator.com/wps/myportal/customer/home/orders/"
MAX_ORDERS = 200

STATUS_META = {
    "ok":           "✓ Downloaded",
    "no_docs":      "⊘ No Documents",
    "not_found":    "⊘ Not Found",
    "timeout":      "✗ Timeout",
    "error":        "✗ Error",
}


def _secret_credentials():
    """Return (username, password) from Streamlit secrets, or (None, None)."""
    try:
        return st.secrets["tke"]["username"], st.secrets["tke"]["password"]
    except Exception:
        return None, None

# ── Core helpers ──────────────────────────────────────────────────────────────

def parse_orders_html(html: str) -> list[dict]:
    """
    Extract order rows from a saved TKE portal Orders page.
    Each row: {"order_id", "equipment_id", "order_date", "order_type"}.
    """
    orders = []
    seen = set()
    # Split on data rows; each contains the id anchor + sibling cells
    chunks = re.split(r'<tr[^>]*name="DataContainer"', html)
    for chunk in chunks[1:]:
        m_id = re.search(r'<a\s+name="id"[^>]*>(\d+)</a>', chunk)
        if not m_id:
            continue
        order_id = m_id.group(1)
        if order_id in seen:
            continue
        seen.add(order_id)
        m_eq   = re.search(r'<span\s+name="equipmentID"[^>]*>(\d*)</span>', chunk)
        m_date = re.search(r'<span\s+name="orderDate"[^>]*>([\d.]+)</span>', chunk)
        m_type = re.search(r'<span\s+name="type"[^>]*>([^<]*)</span>', chunk)
        orders.append({
            "order_id":     order_id,
            "equipment_id": (m_eq.group(1) if m_eq else "") or "",
            "order_date":   (m_date.group(1) if m_date else "") or "",
            "order_type":   (m_type.group(1).strip() if m_type else "") or "",
        })
    return orders


def safe_name(text: str) -> str:
    text = (text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
                .replace("ß", "ss"))
    text = re.sub(r'[<>:"/\\|?*]', "-", text)
    return re.sub(r"\s+", " ", text).strip()


def earliest_from_date(orders: list[dict]) -> str:
    """Return DD.MM.YYYY one month before the oldest order date (defensive search range)."""
    dates = []
    for o in orders:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", o["order_date"])
        if m:
            dates.append((int(m.group(3)), int(m.group(2)), int(m.group(1))))
    if not dates:
        return "01.01.2020"
    y, mo, d = min(dates)
    mo -= 1
    if mo == 0:
        mo, y = 12, y - 1
    return f"01.{mo:02d}.{y}"

# ── Playwright automation ─────────────────────────────────────────────────────

async def _dismiss_cookie_banner(page):
    """Click a cookie-consent button if one is overlaying the login form."""
    for label in ("Alle akzeptieren", "Accept all", "Akzeptieren", "Accept",
                  "Zustimmen", "Einverstanden", "Alle Cookies akzeptieren", "OK"):
        try:
            btn = page.locator(f'button:has-text("{label}")').first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3_000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def _page_diag(page, note: str) -> str:
    """Capture a short description of the current page for failure diagnosis."""
    try:
        url = page.url
        title = await page.title()
        text = await page.evaluate(
            "() => (document.body.innerText || '').replace(/\\s+/g,' ').trim().substring(0, 300)"
        )
        login_visible = await page.locator("#username").count() > 0
        return (f"{note} — url={url[:90]} | title={title!r} | "
                f"login_form_still_visible={login_visible} | page_says={text!r}")
    except Exception as exc:
        return f"{note} — (could not read page: {exc})"


async def _login(page, username: str, password: str):
    """
    Navigate to the portal and authenticate.
    Returns (ok: bool, diagnostic: str); diagnostic is "" on success.
    """
    await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)

    # Already inside (shouldn't happen on a fresh context, but harmless)
    if await page.locator("#ServiceOrderSearchSearchFormid_field").count() > 0:
        return True, ""

    await _dismiss_cookie_banner(page)

    try:
        await page.wait_for_selector("#username", timeout=30_000)
    except Exception:
        return False, await _page_diag(page, "no login form appeared")

    await page.fill("#username", username)
    await page.fill("#password", password)

    # Primary submit: Enter in the password field (fires the form's onkeypress).
    await page.locator("#password").press("Enter")
    try:
        await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=35_000)
        return True, ""
    except Exception:
        pass

    # Fallback submit: click the hidden submit input directly.
    try:
        await page.evaluate(
            """() => {
                const b = document.querySelector('form[name="form.login"] input[type="submit"]')
                       || document.querySelector('input[type="submit"][name="Submit"]');
                if (b) b.click();
            }"""
        )
        await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=35_000)
        return True, ""
    except Exception:
        return False, await _page_diag(page, "credentials submitted but the orders page never loaded")


async def _search_and_open(page, order_id: str, from_date: str) -> str:
    """
    From the Orders page, search one order id and open its detail page.
    Returns "ok" or "not_found".
    """
    await page.goto(PORTAL_ORDERS_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_selector("#ServiceOrderSearchSearchFormid_field", timeout=25_000)

    # Widen the date range so older orders are still found, fill the Id,
    # then trigger the search. All via JS — the form is Dojo-based and
    # responds reliably to value + change events.
    await page.evaluate(
        """([orderId, fromDate]) => {
            const dateField = document.getElementById('ServiceOrderSearchSearchFormorderDateFrom_field');
            if (dateField) {
                dateField.value = fromDate;
                dateField.dispatchEvent(new Event('change', {bubbles: true}));
                dateField.dispatchEvent(new Event('blur', {bubbles: true}));
            }
            const idField = document.getElementById('ServiceOrderSearchSearchFormid_field');
            idField.focus();
            idField.value = orderId;
            idField.dispatchEvent(new Event('change', {bubbles: true}));
            // The portal UI may render in German ("Suchen") or English ("Search").
            const btn = Array.from(document.querySelectorAll('a,button,input'))
                .find(b => {
                    const t = (b.textContent || b.value || '').trim();
                    return t === 'Suchen' || t === 'Search';
                });
            if (!btn) throw new Error('search button not found');
            btn.click();
        }""",
        [order_id, from_date],
    )

    # Wait until the AJAX search result table actually contains the searched id.
    # wait_for_function (not a plain selector) guards against a stale result row
    # from the previous order's search lingering in the DOM.
    try:
        await page.wait_for_function(
            """(orderId) => {
                const rows = Array.from(document.querySelectorAll('a[name="id"]'));
                return rows.length > 0 && rows.some(a => a.textContent.trim() === orderId);
            }""",
            arg=order_id,
            timeout=20_000,
        )
    except Exception:
        return "not_found"

    # Click the row anchor via JS. The portal's id links carry a per-render
    # action token and a native onmouseup handler; Playwright's synthetic
    # locator.click() races the token and lands on the portal error page,
    # so a plain DOM .click() is the reliable trigger here.
    await page.wait_for_timeout(800)
    await page.evaluate(
        """(orderId) => {
            const a = Array.from(document.querySelectorAll('a[name="id"]'))
                .find(x => x.textContent.trim() === orderId)
                || document.querySelector('a[name="id"]');
            a.click();
        }""",
        order_id,
    )

    # Confirm we left the list view (the search field is gone on the detail
    # page) — this is the signal that the detail portlet has loaded.
    try:
        await page.wait_for_selector(
            "#ServiceOrderSearchSearchFormid_field", state="detached", timeout=20_000
        )
    except Exception:
        pass
    return "ok"


async def _collect_documents(page) -> list[dict]:
    """On an order detail page, return [{"href", "doc_type"}] for every öffnen link."""
    # Give the document portlet a moment to render; it may legitimately be empty.
    try:
        await page.wait_for_selector('a[name="External_Url"]', timeout=12_000)
    except Exception:
        return []

    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[name="External_Url"]')).map(a => {
            const tr = a.closest('tr');
            const typeCell = tr ? tr.querySelector('td[name="type_ColumnData"]') : null;
            return {
                href: a.href,
                doc_type: typeCell ? typeCell.textContent.trim() : 'Dokument',
            };
        })"""
    )


async def _download_documents(page, docs: list[dict], order: dict, temp_dir: str) -> list[dict]:
    """Fetch each document href with the session cookies; save only real PDFs."""
    saved = []
    name_counts: dict[str, int] = {}

    for doc in docs:
        base = f"{order['equipment_id']}_{order['order_id']}_{safe_name(doc['doc_type'])}"
        base = base.strip("_")
        name_counts[base] = name_counts.get(base, 0) + 1
        fname = f"{base}.pdf" if name_counts[base] == 1 else f"{base}_{name_counts[base]}.pdf"

        try:
            resp = await page.context.request.get(doc["href"], timeout=30_000)
            body = await resp.body()
            if not body.startswith(b"%PDF"):
                continue
            save_path = os.path.join(temp_dir, fname)
            with open(save_path, "wb") as fh:
                fh.write(body)
            saved.append({"filename": fname, "size_kb": max(1, len(body) // 1024)})
        except Exception:
            continue

    return saved


async def _run_async(orders, username, password, temp_dir: str, state: dict):
    """Login once, then process all orders sequentially in one browser session."""
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    state["total"] = len(orders)
    from_date = earliest_from_date(orders)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        # Present as a normal European desktop Chrome — Playwright's default
        # user agent contains "HeadlessChrome", which many corporate firewalls
        # block. A real UA + German locale/timezone also keeps the portal in
        # the language the rest of the automation expects.
        ctx = await browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"},
        )
        page = await ctx.new_page()

        state["status"] = "Logging in to the TKE portal…"
        ok, diag = await _login(page, username, password)
        if not ok:
            state["error"] = (
                "Login failed. This usually means the portal rejected the "
                "credentials OR blocked the server's location. Diagnostic: "
                + diag
            )
            state["done"] = True
            await browser.close()
            return

        for i, order in enumerate(orders):
            state["current"] = i + 1
            state["status"] = f"[{i+1}/{len(orders)}] Order {order['order_id']}"

            files, status = [], "error"
            try:
                nav = await _search_and_open(page, order["order_id"], from_date)
                if nav == "not_found":
                    status = "not_found"
                else:
                    docs = await _collect_documents(page)
                    if not docs:
                        status = "no_docs"
                    else:
                        files = await _download_documents(page, docs, order, temp_dir)
                        status = "ok" if files else "error"
            except PWTimeout:
                status = "timeout"
            except Exception:
                status = "error"

            state["results"].append({
                "order_id":     order["order_id"],
                "equipment_id": order["equipment_id"],
                "order_date":   order["order_date"],
                "status":       status,
                "files":        files,
                "count":        len(files),
            })

        await ctx.close()
        await browser.close()

    state["done"] = True


def _thread_runner(orders, username, password, temp_dir: str, state: dict):
    """Entry point for the background thread — owns its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async(orders, username, password, temp_dir, state))
    except Exception as exc:
        state["error"] = str(exc)
        state["done"] = True
    finally:
        loop.close()

# ── UI helpers ────────────────────────────────────────────────────────────────

def _results_df(results: list) -> pd.DataFrame:
    rows = []
    for r in results:
        files_str = (
            ", ".join(f"{f['filename']} ({f['size_kb']} KB)" for f in r["files"])
            if r["files"] else "—"
        )
        rows.append({
            "Order":     r["order_id"],
            "Equipment": r["equipment_id"],
            "Date":      r["order_date"],
            "Status":    STATUS_META.get(r["status"], "? Unknown"),
            "PDFs":      r["count"],
            "Files":     files_str,
        })
    return pd.DataFrame(rows)


def _build_zip(temp_dir: str) -> io.BytesIO | None:
    pdfs = sorted(Path(temp_dir).glob("*.pdf"))
    if not pdfs:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in pdfs:
            zf.write(p, p.name)
    buf.seek(0)
    return buf

# ── Session state init ────────────────────────────────────────────────────────

STATE_DEFAULTS = {
    "processing":   False,
    "done":         False,
    "shared_state": None,
    "temp_dir":     None,
    "orders":       None,
}

def _init_state():
    for k, v in STATE_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ── Main app ──────────────────────────────────────────────────────────────────

def main():
    chromium_ok, chromium_log = _install_chromium()
    _init_state()

    st.markdown("""
<div class="hero-section">
    <h1>📥 TKE Service Report Downloader</h1>
    <p>Drop your saved Orders page — get every service report as PDF</p>
</div>
""", unsafe_allow_html=True)

    if not chromium_ok:
        st.error("⚠️ Chromium browser could not be initialised. The app cannot process files.")
        with st.expander("Technical details"):
            st.code(chromium_log)
        return

    # ── DONE state ────────────────────────────────────────────────────────────
    if st.session_state.done:
        shared  = st.session_state.shared_state
        results = shared["results"]

        if shared.get("error") and not results:
            st.error(f"❌ {shared['error']}")
            if st.button("🔄 Try Again"):
                for k, v in STATE_DEFAULTS.items():
                    st.session_state[k] = v
                st.rerun()
            return

        pdf_count  = sum(r["count"] for r in results)
        skip_count = sum(1 for r in results if r["status"] in ("no_docs", "not_found"))
        fail_count = sum(1 for r in results if r["status"] in ("timeout", "error"))

        st.success("✅ Processing complete!")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Orders Processed", len(results))
        c2.metric("✓ PDFs saved",     pdf_count)
        c3.metric("⊘ Skipped",        skip_count)
        c4.metric("✗ Failed",         fail_count)

        st.markdown("### Results")
        st.dataframe(_results_df(results), use_container_width=True, hide_index=True)

        if shared.get("error"):
            st.error(f"An unexpected error occurred: {shared['error']}")

        zip_buf = _build_zip(st.session_state.temp_dir)
        if zip_buf:
            st.markdown("---")
            st.download_button(
                label=f"⬇️  Download all PDFs — {pdf_count} file(s) in ZIP",
                data=zip_buf,
                file_name="tke_service_reports.zip",
                mime="application/zip",
            )
        else:
            st.markdown('<div class="warn-box">⚠️ No PDF files were downloaded successfully.</div>',
                        unsafe_allow_html=True)

        st.markdown("---")
        if st.button("🔄 Start New Batch"):
            if st.session_state.temp_dir and os.path.exists(st.session_state.temp_dir):
                shutil.rmtree(st.session_state.temp_dir, ignore_errors=True)
            for k, v in STATE_DEFAULTS.items():
                st.session_state[k] = v
            st.rerun()
        return

    # ── PROCESSING state ──────────────────────────────────────────────────────
    if st.session_state.processing:
        shared = st.session_state.shared_state
        total  = shared["total"] or len(st.session_state.orders)

        st.markdown("### ⏳ Downloading Reports…")
        st.markdown(
            '<div class="info-box">🔄 Processing is running. '
            'Please <strong>keep this tab open</strong> until complete.</div>',
            unsafe_allow_html=True,
        )

        progress_bar      = st.progress(0.0)
        status_text       = st.empty()
        results_container = st.empty()

        while not shared["done"]:
            current = shared["current"]
            pct     = current / total if total > 0 else 0.0
            progress_bar.progress(min(pct, 1.0))
            status_text.markdown(f"**{shared['status']}**")

            if shared["results"]:
                with results_container.container():
                    st.dataframe(_results_df(shared["results"]),
                                 use_container_width=True, hide_index=True)
            time.sleep(0.5)

        progress_bar.progress(1.0)
        status_text.markdown("**✅ Done!**")

        st.session_state.done       = True
        st.session_state.processing = False
        st.rerun()
        return

    # ── UPLOAD state ─────────────────────────────────────────────────────────
    st.markdown("### 📂 Upload Saved Orders Page")
    st.markdown(
        '<div class="info-box">💡 In the TKE portal, open <strong>Orders</strong>, '
        'set your date range, then save the page with <strong>Ctrl+S</strong> '
        '(format: "Webpage, HTML Only") and drop the .html file here.</div>',
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        "Drag & drop the saved Orders .html page here",
        type=["html", "htm"],
        accept_multiple_files=False,
        label_visibility="collapsed",
    )

    if not uploaded:
        return

    html = uploaded.getvalue().decode("utf-8", errors="ignore")
    orders = parse_orders_html(html)

    if not orders:
        st.error("No order IDs found in this file. Make sure you saved the portal's "
                 "Orders page (the table with the Id column must be visible).")
        return

    if len(orders) > MAX_ORDERS:
        st.markdown(
            f'<div class="warn-box">⚠️ {len(orders)} orders found. '
            f'Only the first {MAX_ORDERS} will be processed.</div>',
            unsafe_allow_html=True,
        )
        orders = orders[:MAX_ORDERS]

    st.markdown("### 📋 Orders Found")
    preview = pd.DataFrame([{
        "Order":     o["order_id"],
        "Date":      o["order_date"],
        "Type":      o["order_type"],
        "Equipment": o["equipment_id"],
    } for o in orders])
    st.dataframe(preview, use_container_width=True, hide_index=True, height=300)

    c1, c2 = st.columns(2)
    c1.metric("Orders Found", len(orders))
    c2.metric("Date Range", f"{orders[-1]['order_date']} – {orders[0]['order_date']}"
              if orders[0]["order_date"] else "—")

    # ── Portal login ──────────────────────────────────────────────────────────
    sec_user, sec_pass = _secret_credentials()

    if sec_user and sec_pass:
        username, password = sec_user, sec_pass
        st.markdown(
            '<div class="info-box">🔐 Signed in automatically with the configured '
            'portal account — no login needed.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown("### 🔐 Portal Login")
        st.markdown(
            '<div class="info-box">The portal links require an active login session, '
            'so the app signs in with your portal credentials. They are used for this '
            'batch only and are <strong>never stored</strong>.</div>',
            unsafe_allow_html=True,
        )
        lc1, lc2 = st.columns(2)
        username = lc1.text_input("Username", placeholder="Benutzername")
        password = lc2.text_input("Password", type="password", placeholder="Passwort")

    st.markdown("---")
    start = st.button("🚀 Start Download", type="primary",
                      disabled=not (username and password))

    if start:
        temp_dir = tempfile.mkdtemp()
        shared_state = {
            "current": 0, "total": 0,
            "status": "Starting…",
            "results": [], "done": False, "error": None,
        }
        st.session_state.processing   = True
        st.session_state.done         = False
        st.session_state.shared_state = shared_state
        st.session_state.temp_dir     = temp_dir
        st.session_state.orders       = orders

        threading.Thread(
            target=_thread_runner,
            args=(orders, username, password, temp_dir, shared_state),
            daemon=True,
        ).start()
        st.rerun()


if __name__ == "__main__":
    main()

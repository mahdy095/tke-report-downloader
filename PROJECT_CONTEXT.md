# TKE Service Report Downloader — Project Context

## What this app does

A publicly deployed Streamlit web app that automates downloading PDF service
reports from the **TK Elevator (TKE) customer portal**
(`de.webportal.tkelevator.com`).

The user logs into the portal manually in their browser, opens the **Orders**
(Aufträge) page, and saves it with **Ctrl+S → "Webpage, HTML Only"**. They then
drop that saved `.html` file into this app. The app:

1. Parses every order row out of the saved HTML (Order Id, Equipment Id, date, type).
2. Launches a headless Chromium browser (Playwright) server-side.
3. Logs into the portal with stored credentials (Streamlit secrets).
4. For each order: searches the Id in the Orders search form, opens the detail
   page, and collects every "öffnen" document link.
5. Fetches each document with the authenticated session and keeps only real PDFs.
6. Bundles all PDFs into a single ZIP for download.

No installation required on the user's device — everything runs on Streamlit Cloud.

**GitHub:** https://github.com/mahdy095/tke-report-downloader
**Live URL:** (set after deployment on Streamlit Cloud)

---

## Repository structure

```
tke_downloader/
├── app.py                       # Single-file Streamlit app
├── requirements.txt             # Python dependencies
├── packages.txt                 # System apt packages for Chromium on Debian Trixie
├── runtime.txt                  # Pins Python 3.11
├── .streamlit/
│   ├── config.toml              # maxUploadSize, theme colours
│   └── secrets.toml.example     # Template for portal credentials (real one is gitignored)
└── .gitignore
```

This app is a sibling of the **KONE Service Report Downloader** and the
**Lift Components** apps — same single-file Streamlit architecture, same
Playwright-in-a-background-thread pattern, same Inter/indigo design system
(TKE uses a purple `#7c3aed` hero instead of KONE's indigo).

---

## Credentials (Streamlit secrets)

The portal links are session-bound — every order link and every PDF link
redirects to the login page without an authenticated session. The app therefore
signs in server-side before processing.

Credentials live in `.streamlit/secrets.toml` (gitignored, never committed):

```toml
[tke]
username = "your-portal-username@example.com"
password = "your-portal-password"
```

On Streamlit Cloud, paste the same block into **App → Settings → Secrets**.

`_secret_credentials()` reads `st.secrets["tke"]`. If secrets are present the app
auto-logs-in (a green "signed in automatically" banner shows, no fields). If
secrets are **absent**, the app falls back to showing Username/Password input
fields so it still works without secrets.

---

## Deployment environment

- **Platform:** Streamlit Cloud (free tier)
- **OS:** Debian Trixie (NOT Ubuntu — important for package names)
- **Python:** 3.11 (pinned via `runtime.txt`)
- **Auto-deploy:** pushes to `main` trigger redeployment
- **Secrets required:** `[tke]` username/password (see above)

### packages.txt (Chromium system libs on Debian Trixie)

Same working set as the KONE app. Do **not** include `libglib2.0-0` — it
conflicts with the Trixie `libglib2.0-0t64` variant pulled in by `libatk1.0-0`.

### requirements.txt

```
streamlit>=1.29.0
playwright>=1.40.0
pandas>=2.0.0
```

(No `extract-msg` — unlike the KONE app, this app parses HTML, not .msg files.)

---

## Playwright on Streamlit Cloud

Identical pattern to the KONE app — the Chromium binary is downloaded at runtime
(it can't be committed to git) and cached so it runs once per deployment:

```python
@st.cache_resource(show_spinner=False)
def _install_chromium():
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True, timeout=180,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()
```

Launch flags on Linux: `chromium.launch(headless=True, args=["--no-sandbox"])`.

---

## asyncio + threading pattern (critical for Streamlit)

Same as the KONE app. Streamlit's main thread can't run `asyncio.run()`, so a
daemon thread owns its own event loop and writes progress into a shared `state`
dict stored in `st.session_state`. The main thread polls it in a blocking
`while not state["done"]: time.sleep(0.5)` loop, refreshing `st.empty()`
containers each iteration to stream live updates.

---

## App flow / UI states

Three mutually-exclusive states controlled by `st.session_state`:

### State 1: UPLOAD (default)
- `st.file_uploader` (single file, `type=["html","htm"]`, `label_visibility="collapsed"`)
- On upload: `parse_orders_html()` extracts all order rows → preview table + metrics
- Credentials: auto from secrets (banner) OR username/password fields if no secrets
- `🚀 Start Download` button (disabled until credentials present) → PROCESSING

### State 2: PROCESSING
- Blocking `while not shared["done"]: time.sleep(0.5)` loop in main thread
- `st.progress()` bar + live results dataframe in `st.empty()`
- On completion → `done = True` → `st.rerun()` → DONE

### State 3: DONE
- Summary metrics (Orders Processed / PDFs saved / Skipped / Failed)
- Full results dataframe
- `st.download_button` for the ZIP
- `🔄 Start New Batch` clears session state, removes temp dir, reruns

### shared_state dict
```python
{"current": int, "total": int, "status": str,
 "results": list, "done": bool, "error": str|None}
```

### Each result dict
```python
{"order_id": str, "equipment_id": str, "order_date": str,
 "status": str,   # "ok" | "no_docs" | "not_found" | "timeout" | "error"
 "files": list,   # [{"filename": str, "size_kb": int}, ...]
 "count": int}
```

---

## HTML parsing (`parse_orders_html`)

The saved Orders page is split on `<tr ... name="DataContainer"`; each row chunk
yields one order:

```python
re.search(r'<a\s+name="id"[^>]*>(\d+)</a>', chunk)                 # order_id
re.search(r'<span\s+name="equipmentID"[^>]*>(\d*)</span>', chunk)  # equipment_id
re.search(r'<span\s+name="orderDate"[^>]*>([\d.]+)</span>', chunk) # order_date (DD.MM.YYYY)
re.search(r'<span\s+name="type"[^>]*>([^<]*)</span>', chunk)       # order_type
```

Duplicate ids are de-duplicated. Verified: 100/100 orders parsed from a real
saved page; ~7 had no equipment id (legitimately blank in the portal), handled
gracefully.

---

## Portal automation logic (the hard-won part)

Single browser session, single page, login once, then sequential orders.

### Login (`_login`)
Go to the Orders URL → if redirected to the login form, fill `#username` /
`#password` and press Enter → wait for `#ServiceOrderSearchSearchFormid_field`
(the Id search box) to confirm we're in. No captcha.

### Search & open one order (`_search_and_open`)
1. `page.goto(PORTAL_ORDERS_URL)` fresh, wait for the Id search field.
2. Via `page.evaluate`: set the **from-date** field (widen the range so older
   orders are found), set the **Id** field, dispatch `change`, click the search
   button. **The button text is language-dependent — match both `"Suchen"`
   (German) and `"Search"` (English).**
3. `wait_for_function` until the result table actually contains an `a[name="id"]`
   whose text equals the searched id (guards against a stale row from the
   previous order's search).
4. Click the result anchor via **plain DOM `.click()` inside `page.evaluate`** —
   NOT Playwright's `locator.click()`.
5. Wait for `#ServiceOrderSearchSearchFormid_field` to be **detached** (signals
   we've left the list and the detail portlet has loaded).

### Collect documents (`_collect_documents`)
Wait up to 12 s for `a[name="External_Url"]`; return `[]` (→ `no_docs`) if none.
Each row's document type comes from the sibling `td[name="type_ColumnData"]`
(e.g. "Checkliste", "Wartungsbestätigung").

### Download documents (`_download_documents`)
Fetch each href with `page.context.request.get(...)` (carries session cookies),
check the body starts with `b"%PDF"`, and save. Non-PDF responses are skipped.

**Filename format:** `{equipment_id}_{order_id}_{safe(doc_type)}.pdf`
e.g. `287305033_409869505_Wartungsbestaetigung.pdf`. German umlauts are
transliterated (ä→ae, ö→oe, ü→ue, ß→ss). On collision a `_2`, `_3` suffix is added.

---

## Known gotchas & lessons learned

1. **The portal renders in GERMAN headless.** A logged-in user's Chrome may show
   English ("Search"/"Reset") due to a language cookie, but a fresh Playwright
   context gets German ("Suchen"/"Zurücksetzen"). Any text-based element lookup
   must match both languages. This was the original "everything errors" bug.

2. **Dates must be `DD.MM.YYYY`.** The German date field rejects `M/D/YYYY`.
   `earliest_from_date()` returns `01.MM.YYYY` one month before the oldest order.

3. **Use DOM `.click()`, not `locator.click()`, on the order Id link.** The id
   anchors carry a per-render action token + native `onmouseup` handler;
   Playwright's synthetic click races the token and lands on the portal error
   page ("Ein Fehler ist aufgetreten"). A plain `element.click()` via
   `page.evaluate` fires the handler correctly.

4. **Wait for definitive signals between steps, not `domcontentloaded`.** The
   portal navigations are AJAX/portlet swaps. Two races caused intermittent
   `no_docs` / `not_found`: (a) collecting documents before the detail page
   rendered, (b) reading search results before the new search replaced the old.
   Fixed with `wait_for_function` (result contains the searched id) and
   `wait_for_selector(state="detached")` (left the list view).

5. **Session-bound links.** Every order/PDF link 302s to the login page without
   cookies — the server-side login is mandatory, the saved HTML alone is not
   enough to fetch the PDFs.

6. **Material Icons CSS fix** (shared design-system gotcha): the global
   `* { font-family: 'Inter' }` override must be followed by
   `[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' }`
   or icon glyphs render as literal text.

7. **secrets.toml is gitignored.** Real credentials never go to GitHub. The
   committed `secrets.toml.example` is the template; the live values are pasted
   into Streamlit Cloud's Secrets box.

---

## Verification done

End-to-end tested headless against the live portal with real credentials:
- 3-order batch: 3/3 ok, 6/6 valid PDFs.
- 8-order batch: 8/8 ok, 16/16 valid PDFs, correct names, zero failures.
- HTML parser: 100/100 orders extracted from a real saved Orders page.

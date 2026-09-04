#!/usr/bin/env python3
"""
ISB CAS-KMP resume scraper  (v2 -- fixes run_search returning None)

    python isb_kmp_scraper.py login
    python isb_kmp_scraper.py run --years 2025 --company "Flipkart India Pvt. Ltd."
    python isb_kmp_scraper.py run --years 2024 2025 2026 --company "~Flipkart"
    python isb_kmp_scraper.py inspect
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Page, BrowserContext

# ============================================================
#  EDIT ME -- leave a value as None or "" to skip that filter.
#  Class years come from `run --years`. Company comes from
#  `run --company` (overrides FILTERS["post_isb_company"]).
#  Do not set post_isb_function / post_isb_designation here: the
#  portal dropdowns are unreliable. RESULT_NEEDLE filters those
#  fields on the results page instead (substring, case-insensitive).
# ============================================================
FILTERS = {
    "class":                None,            # overridden by --years
    "post_isb_industry":    "All",           # required
    "pre_isb_company":      None,
    "pre_isb_experience":   None,
    "pre_isb_industry":     None,
    "pre_isb_function":     None,
    "pre_isb_designation":  None,
    "post_isb_function":    None,
    "post_isb_company":     None,            # overridden by --company
    "post_isb_designation": None,
}

# Keep a results-page card if this text appears in Post ISB Function
# OR Post ISB Designation. Set to None/"" to download every resume.
RESULT_NEEDLE = "consulting"
PATTERN_PREFIX = "~"
OPTIONS_FILE = Path("kmp_options.json")

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BASE = "https://apps.isb.edu/CAS-KMP/"
SEARCH_PAGE = urljoin(BASE, "Admin/Searchstudent.aspx")

PROFILE_DIR = Path.home() / ".isb_kmp_profile"
OUT_ROOT = Path("cvs")

CONCURRENCY = 3
DELAY_RANGE = (0.3, 0.9)

CLASS_FIELD_KEYWORDS = ["preisbclass"]        # MainContent_lstBoxPreIsbClass
INDUSTRY_FIELD_KEYWORDS = ["postisbindustry"] # MainContent_lstBoxPostIsbIndustry

NEXT_PAGE_SELECTORS = [
    "a[rel='next']",
    "a[title='Next']",
    "li.next:not(.disabled) a",
    ".pagination a:has-text('Next')",
    "a:has-text('Next')",
    "input[type='submit'][value='Next']",
    "a:has-text('>>')",
]

# ----------------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------------

DISPLAY_LINK_RE = re.compile(r"DisplayFile\.aspx\?id=([A-Za-z0-9_\-.]+)", re.I)
PDF_URL_RE = re.compile(r"""["'(\s]([^"'()\s<>]+?\.pdf(?:\?[^"'()\s<>]*)?)""", re.I)
TOTAL_RE = re.compile(r"Total\s*no\.?\s*of\s*records\s*:?\s*([\d,]+)", re.I)
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-]+")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

async def ainput(prompt: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, prompt)


def log(msg: str) -> None:
    print(msg, flush=True)


async def open_context(pw, headless: bool) -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
    )


async def looks_logged_out(page: Page) -> bool:
    if re.search(r"(login|signin|sso|adfs|okta|auth)", page.url, re.I):
        return True
    return await page.locator("input[type='password']").count() > 0


async def collect_html(p: Page) -> str:
    """Main document HTML plus every iframe's HTML."""
    parts = []
    try:
        parts.append(await p.content())
    except Exception:
        pass
    for f in p.frames:
        if f is p.main_frame:
            continue
        try:
            parts.append(await f.content())
        except Exception:
            pass
    return "\n".join(parts)


async def switch_to_results(context: BrowserContext, page: Page) -> Page:
    """Search may open results in a new tab -- find whichever page has them."""
    await page.wait_for_timeout(2500)
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        return page

    for p in reversed(pages):          # newest tab first
        try:
            await p.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        html = await collect_html(p)
        if DISPLAY_LINK_RE.search(html) or TOTAL_RE.search(html):
            if p is not page:
                log(f"    results opened in a new tab: {p.url}")
            return p

    log("    no tab contained recognisable results; using the newest one")
    return pages[-1]


# ----------------------------------------------------------------------------
# Form driving
# ----------------------------------------------------------------------------

SET_SELECT_JS = """
([keywords, values]) => {
  const selects = Array.from(document.querySelectorAll('select'));
  const hay = s => ((s.id || '') + ' ' + (s.name || '')).toLowerCase();
  const sel = selects.find(s => keywords.every(k => hay(s).includes(k)));
  if (!sel) return {ok: false, reason: 'no matching select'};

  const norm = t => String(t == null ? '' : t).replace(/\u00a0/g, ' ').trim().toLowerCase();
  const wanted = values.map(norm);
  const opts = Array.from(sel.options).filter(
    o => wanted.includes(norm(o.text)) || wanted.includes(norm(o.value))
  );
  if (opts.length === 0) {
    return {ok: false, reason: 'no option matched ' + JSON.stringify(values),
            options: Array.from(sel.options).slice(0, 40).map(o => o.text.trim())};
  }

  Array.from(sel.options).forEach(o => { o.selected = false; });
  opts.forEach(o => { o.selected = true; });
  if (!sel.multiple) sel.value = opts[0].value;

  if (window.jQuery) { window.jQuery(sel).trigger('change'); }
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, id: sel.id || sel.name,
          selected: opts.map(o => o.text.trim()).join(', '),
          multiple: sel.multiple};
}
"""


async def set_select(page: Page, keywords, values) -> bool:
    if isinstance(keywords, str):
        keywords = [keywords]
    if isinstance(values, (str, int)):
        values = [values]
    values = [str(v) for v in values]
    res = await page.evaluate(SET_SELECT_JS, [keywords, values])
    if res.get("ok"):
        log(f"    set {res['id']} -> {res['selected']}")
        return True
    log(f"    could not set {keywords} = {values!r} ({res.get('reason')})")
    if res.get("options"):
        log(f"      available: {res['options']}")
    return False


async def click_search(page: Page) -> bool:
    for sel in [
        "input[type='submit'][value*='Search' i]",
        "button:has-text('Search')",
        "input[type='button'][value*='Search' i]",
        "a:has-text('Search')",
    ]:
        loc = page.locator(sel).first
        if await loc.count():
            log(f"    clicking search via {sel}")
            await loc.click()
            return True
    log("    no search button found")
    return False


async def run_search(context: BrowserContext, page: Page, year: int, manual: bool, plan: list) -> Page:
    """Always returns the Page that holds the results."""
    log(f"  opening search form: {SEARCH_PAGE}")
    await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")

    if await looks_logged_out(page):
        await ainput("  Not logged in. Sign in in the browser window, then press Enter... ")
        await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")

    if manual:
        await ainput(
            f"  MANUAL MODE: set Post-ISB Industry = All and Class = {year},\n"
            f"  hit Search, wait for the results, then press Enter here... "
        )
        return await switch_to_results(context, page)

    order = ["class", "post_isb_industry", "pre_isb_industry", "pre_isb_function",
             "pre_isb_company", "pre_isb_experience", "pre_isb_designation",
             "post_isb_function", "post_isb_company", "post_isb_designation"]
    plan.sort(key=lambda t: order.index(t[0]) if t[0] in order else 99)

    all_ok = True
    for key, frag, values in plan:
        if not await set_select(page, [frag], values):
            all_ok = False
        await page.wait_for_timeout(600)

    if not (all_ok and await click_search(page)):
        await ainput(
            "  Auto-fill failed. Set the filters and hit Search manually,\n"
            "  then press Enter here... "
        )
        return await switch_to_results(context, page)

    try:
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception:
        pass

    return await switch_to_results(context, page)


# ----------------------------------------------------------------------------
# Results harvesting
# ----------------------------------------------------------------------------

EXTRACT_CARDS_JS = """() => {
  const norm = s => String(s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const cards = [];
  const root = document.querySelector('#div_studentreport') || document;
  root.querySelectorAll('.post').forEach(post => {
    const get = (wanted) => {
      for (const g of post.querySelectorAll('.form-groupy')) {
        const lab = g.querySelector('label.labely');
        if (!lab) continue;
        if (norm(lab.innerText).toLowerCase() !== wanted) continue;
        const font = g.querySelector('font');
        return font ? norm(font.innerText) : '';
      }
      return '';
    };
    const designation = get('post isb designation');
    const func = get('post isb function');
    if (!designation && !func) return;
    const a = post.querySelector('a[href*="DisplayFile.aspx"]');
    let id = null;
    if (a) {
      const m = (a.getAttribute('href') || '').match(/DisplayFile\\.aspx\\?id=([^&]+)/i);
      if (m) id = m[1];
    }
    cards.push({
      company: get('post isb company'),
      designation,
      function: func,
      klass: get('class'),
      id,
    });
  });
  return cards;
}"""


def card_matches_needle(card: dict, needle: str | None) -> bool:
    if not needle:
        return True
    n = needle.strip().lower()
    if not n:
        return True
    des = (card.get("designation") or "").lower()
    func = (card.get("function") or "").lower()
    return n in des or n in func


async def harvest_ids(page: Page, needle: str | None = None) -> list[str]:
    if page is None:
        log("  no results page to harvest from")
        return []

    ids: list[str] = []
    seen: set = set()
    page_no = 1
    skipped = 0

    log(f"  harvesting from: {page.url}")
    if needle:
        log(f"  keeping cards where designation or function contains {needle!r}")

    while True:
        for _ in range(6):
            try:
                await page.mouse.wheel(0, 20000)
            except Exception:
                break
            await page.wait_for_timeout(250)

        html = await collect_html(page)

        if page_no == 1:
            m = TOTAL_RE.search(html)
            if m:
                log(f"  portal reports {m.group(1)} total records")

        try:
            cards = []
            seen_cards: set = set()
            for frame in [page.main_frame, *page.frames]:
                try:
                    batch = await frame.evaluate(EXTRACT_CARDS_JS)
                except Exception:
                    continue
                for card in batch or []:
                    key = (card.get("id"), card.get("designation"), card.get("function"))
                    if key in seen_cards:
                        continue
                    seen_cards.add(key)
                    cards.append(card)
        except Exception as e:
            log(f"  could not parse result cards ({e}); falling back to raw View links")
            cards = [{"id": rid, "designation": "", "function": ""}
                     for rid in DISPLAY_LINK_RE.findall(html)]

        new = 0
        for card in cards:
            rid = card.get("id")
            if not card_matches_needle(card, needle):
                skipped += 1
                log(f"    skip  {card.get('designation') or '—'} | {card.get('function') or '—'}")
                continue
            if not rid:
                skipped += 1
                log(f"    skip (no resume)  {card.get('designation') or '—'} | {card.get('function') or '—'}")
                continue
            if rid in seen:
                continue
            seen.add(rid)
            ids.append(rid)
            new += 1
            log(f"    keep  {card.get('designation') or '—'} | {card.get('function') or '—'} -> {rid}")

        log(f"  page {page_no}: +{new} kept (running total {len(ids)}, skipped {skipped})")

        if new == 0 and page_no > 1:
            break

        clicked = False
        for sel in NEXT_PAGE_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_enabled():
                    await loc.click()
                    await page.wait_for_load_state("networkidle", timeout=30000)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            break
        page_no += 1

    if not ids:
        Path("debug_results.html").write_text(await collect_html(page))
        log(f"  no matching resumes -- wrote debug_results.html from {page.url}")

    return ids


# ----------------------------------------------------------------------------
# PDF resolution + download
# ----------------------------------------------------------------------------

def extract_pdf_url(html: str, page_url: str):
    candidates: list = []

    for attr in ("src", "data", "href"):
        for m in re.finditer(
            rf"""<(?:iframe|embed|object|a)\b[^>]*\b{attr}\s*=\s*["']([^"']+)["']""",
            html, re.I,
        ):
            if ".pdf" in m.group(1).lower():
                candidates.append(m.group(1))

    candidates += PDF_URL_RE.findall(html)

    for c in candidates:
        if "pdf_embedder" in c or c.startswith("chrome-extension"):
            continue
        return urljoin(page_url, c.replace("&amp;", "&"))
    return None


def safe_folder(name: str) -> str:
    s = SAFE_NAME_RE.sub("_", (name or "").strip())
    s = re.sub(r"_+", "_", s).strip("._")
    return s or "company"


def company_from_filters(filters: dict) -> str:
    val = filters.get("post_isb_company")
    if isinstance(val, list):
        val = val[0] if val else None
    if not val:
        return "unspecified"
    return str(val)


def apply_company_flag(filters: dict, companies: list[str] | None) -> dict:
    out = dict(filters)
    if not companies:
        return out
    if any(c.startswith(PATTERN_PREFIX) for c in companies) and len(companies) > 1:
        raise SystemExit("--company: pass one ~pattern, or several exact names, not both")
    out["post_isb_company"] = companies[0] if len(companies) == 1 else companies
    return out


def safe_filename(url: str, record_id: str) -> str:
    name = Path(urlparse(url).path).name or f"{record_id}.pdf"
    name = SAFE_NAME_RE.sub("_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


async def fetch_one(context, record_id, outdir, manifest, sem, counter) -> None:
    async with sem:
        counter["done"] += 1
        n, total = counter["done"], counter["total"]

        if record_id in manifest and manifest[record_id].get("status") == "ok":
            log(f"  [{n}/{total}] {record_id} already downloaded, skipping")
            return

        display_url = urljoin(BASE, f"DisplayFile.aspx?id={record_id}")
        try:
            resp = await context.request.get(display_url, timeout=45000)
            if not resp.ok:
                raise RuntimeError(f"DisplayFile HTTP {resp.status}")

            body = await resp.text()
            pdf_url = extract_pdf_url(body, display_url)

            if pdf_url is None:
                ctype = (resp.headers.get("content-type") or "").lower()
                if "pdf" in ctype:
                    data = await resp.body()
                    fname = f"{record_id}.pdf"
                    (outdir / fname).write_bytes(data)
                    manifest[record_id] = {"status": "ok", "file": fname, "src": display_url}
                    log(f"  [{n}/{total}] {record_id} -> {fname} (direct)")
                    return
                raise RuntimeError("no PDF url found in DisplayFile page")

            pdf_resp = await context.request.get(pdf_url, timeout=90000)
            if not pdf_resp.ok:
                raise RuntimeError(f"PDF HTTP {pdf_resp.status}")

            data = await pdf_resp.body()
            if not data.startswith(b"%PDF"):
                raise RuntimeError("response was not a PDF (session expired?)")

            fname = safe_filename(pdf_url, record_id)
            target = outdir / fname
            if target.exists():
                target = outdir / f"{target.stem}__{record_id}.pdf"
            target.write_bytes(data)

            manifest[record_id] = {"status": "ok", "file": target.name, "src": pdf_url}
            log(f"  [{n}/{total}] {record_id} -> {target.name}")

        except Exception as e:
            manifest[record_id] = {"status": "error", "error": str(e)}
            log(f"  [{n}/{total}] {record_id} FAILED: {e}")

        await asyncio.sleep(random.uniform(*DELAY_RANGE))


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

async def cmd_login() -> None:
    async with async_playwright() as pw:
        context = await open_context(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")
        await ainput("Log in, land on the search form, then press Enter... ")
        log("Session saved.")
        await context.close()


async def cmd_inspect() -> None:
    async with async_playwright() as pw:
        context = await open_context(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")
        if await looks_logged_out(page):
            await ainput("Log in, navigate to the search form, then press Enter... ")

        dump = await page.evaluate("""() => {
          const out = {selects: [], inputs: [], buttons: []};
          document.querySelectorAll('select').forEach(s => out.selects.push({
            id: s.id, name: s.name, multiple: s.multiple,
            options: Array.from(s.options).slice(0, 60).map(o => o.text.trim())
          }));
          document.querySelectorAll('input').forEach(i => out.inputs.push({
            id: i.id, name: i.name, type: i.type, value: i.value
          }));
          document.querySelectorAll('button, input[type=submit], input[type=button]')
            .forEach(b => out.buttons.push({id: b.id, name: b.name,
                                            text: (b.innerText || b.value || '').trim()}));
          return out;
        }""")

        Path("form_dump.json").write_text(json.dumps(dump, indent=2))
        log(f"Wrote form_dump.json ({len(dump['selects'])} selects).")
        await context.close()

FIELD_MAP = {
    "pre_isb_company":      "precompanyname",
    "pre_isb_experience":   "preisbexp",
    "pre_isb_industry":     "preisbindustry",
    "pre_isb_function":     "preisbfunction",
    "pre_isb_designation":  "preisbdesignation",
    "class":                "preisbclass",
    "post_isb_industry":    "postisbindustry",
    "post_isb_function":    "postisbfunction",
    "post_isb_company":     "postcompanyname",
    "post_isb_designation": "postisbdesignation",
}


SCRAPE_SELECTS_JS = """() => {
  const out = {};
  document.querySelectorAll('select').forEach(s => {
    const key = ((s.id || '') + ' ' + (s.name || '')).toLowerCase();
    out[key] = {
      id: s.id || s.name,
      multiple: s.multiple,
      options: Array.from(s.options).map(o =>
        (o.text || '').replace(/\\u00a0/g, ' ').trim()
      ).filter(Boolean)
    };
  });
  return out;
}"""


def map_selects(raw: dict) -> dict:
    options = {}
    for friendly, frag in FIELD_MAP.items():
        hit = next((v for k, v in raw.items() if frag in k), None)
        if hit:
            options[friendly] = hit
            log(f"  {friendly:22s} <- {hit['id']} ({len(hit['options'])} options)")
        else:
            log(f"  {friendly:22s} NOT FOUND (adjust FIELD_MAP)")
    return options


async def scrape_options(page: Page) -> dict:
    await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")
    if await looks_logged_out(page):
        await ainput("Log in, land on the search form, then press Enter... ")
        await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")

    raw = await page.evaluate(SCRAPE_SELECTS_JS)
    options = map_selects(raw)

    Path("all_selects.json").write_text(json.dumps(
        {v["id"]: {"multiple": v["multiple"], "count": len(v["options"]),
                   "sample": v["options"][:5]}
         for v in raw.values()}, indent=2))
    OPTIONS_FILE.write_text(json.dumps(options, indent=2))
    log(f"Wrote {OPTIONS_FILE} with {len(options)} fields.")
    return options


def pattern_fields(filters: dict) -> list[str]:
    return [
        k for k, v in filters.items()
        if isinstance(v, str) and v.startswith(PATTERN_PREFIX)
    ]


async def cmd_export_options() -> None:
    async with async_playwright() as pw:
        context = await open_context(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await scrape_options(page)
        await context.close()


async def cmd_run(years, manual, headless, limit, companies) -> None:
    filters_in = apply_company_flag(FILTERS, companies)
    if not filters_in.get("post_isb_company"):
        raise SystemExit("pass --company NAME (or set FILTERS['post_isb_company'])")

    options = load_options()
    needed = pattern_fields(filters_in)
    missing = [k for k in needed if k not in options]

    async with async_playwright() as pw:
        context = await open_context(pw, headless=headless and not manual)
        page = context.pages[0] if context.pages else await context.new_page()

        if missing:
            log(f"{OPTIONS_FILE} is missing {missing} -- scraping dropdowns from the search form.")
            options = await scrape_options(page)

        base, iter_field, iter_values = expand_iterables(filters_in, options)

        if iter_field:
            log(f"\n{iter_field} matches {len(iter_values)} options:")
            for v in iter_values:
                log(f"   - {v}")

        log(f"Class years from --years: {', '.join(str(y) for y in years)}")
        if RESULT_NEEDLE:
            log(f"Results filter: designation or function contains {RESULT_NEEDLE!r}")

        for year in years:
            log(f"\n=== Class of {year} ===")
            year_ok = year_n = 0
            year_dirs: list[Path] = []

            variants = ([{iter_field: v} for v in iter_values] if iter_field else [{}])

            for i, extra in enumerate(variants, 1):
                label = extra.get(iter_field, "") if iter_field else ""
                if label:
                    log(f"\n--- [{i}/{len(variants)}] {label}")

                filters = {**base, "class": [year], **extra}
                plan = validate_filters(filters, options)

                company_name = company_from_filters(filters)
                outdir = OUT_ROOT / str(year) / safe_folder(company_name)
                outdir.mkdir(parents=True, exist_ok=True)
                year_dirs.append(outdir)
                manifest_path = outdir / "_manifest.json"
                manifest = load_manifest(manifest_path)
                log(f"  saving to {outdir}")

                results = await run_search(context, page, year, manual, plan)
                ids = await harvest_ids(results, RESULT_NEEDLE)
                if limit:
                    ids = ids[:limit]
                log(f"  collected {len(ids)} record ids")

                if not ids:
                    year_n += len(manifest)
                    year_ok += sum(1 for v in manifest.values() if v.get("status") == "ok")
                    continue

                sem = asyncio.Semaphore(CONCURRENCY)
                counter = {"done": 0, "total": len(ids)}
                try:
                    await asyncio.gather(*[
                        fetch_one(context, rid, outdir, manifest, sem, counter) for rid in ids
                    ])
                finally:
                    manifest_path.write_text(json.dumps(manifest, indent=2))

                year_n += len(manifest)
                year_ok += sum(1 for v in manifest.values() if v.get("status") == "ok")

            shown = year_dirs[0] if len(year_dirs) == 1 else OUT_ROOT / str(year)
            log(f"\n  {year}: {year_ok} downloaded, {year_n - year_ok} failed -> {shown}")

        await context.close()


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log(f"  {path} is not valid JSON; starting a fresh manifest")
        return {}
    return data if isinstance(data, dict) else {}


def load_options() -> dict:
    if not OPTIONS_FILE.exists():
        return {}
    return json.loads(OPTIONS_FILE.read_text())


def validate_filters(filters: dict, options: dict) -> list:
    """Returns [(field_key, id_fragment, [values]), ...]; raises on bad input."""
    problems, plan = [], []

    for key, val in filters.items():
        if key not in FIELD_MAP:
            problems.append(f"unknown field '{key}'")
            continue
        if val is None or val == "" or val == []:
            continue

        values = [str(v) for v in (val if isinstance(val, list) else [val])]

        if key in options:
            valid = options[key]["options"]
            lower = {v.lower(): v for v in valid}
            for v in values:
                if v.lower() not in lower:
                    close = [c for c in valid if v.lower() in c.lower()][:5]
                    msg = f"'{v}' is not valid for {key}"
                    if close:
                        msg += f" -- did you mean: {close}?"
                    problems.append(msg)
            if not options[key]["multiple"] and len(values) > 1:
                problems.append(f"{key} accepts only one value, got {len(values)}")

        plan.append((key, FIELD_MAP[key], values))

    for required in ("class", "post_isb_industry"):
        if not filters.get(required):
            problems.append(f"{required} is mandatory and cannot be empty")

    if problems:
        raise SystemExit("Filter errors:\n  - " + "\n  - ".join(problems))
    return plan

NEUTRAL = {"select", "all", "-", ".", ""}


def expand_iterables(filters: dict, options: dict):
    """A value like '~walmart' becomes a loop over every matching option.
    Returns (base_filters, iter_field, iter_values)."""
    base, iter_field, iter_values = {}, None, []

    for key, val in filters.items():
        if isinstance(val, str) and val.startswith(PATTERN_PREFIX):
            needle = val[len(PATTERN_PREFIX):].strip().lower()
            if not needle:
                raise SystemExit(f"{key}: empty pattern after '{PATTERN_PREFIX}'")
            if key not in options:
                raise SystemExit(
                    f"{key}: not in kmp_options.json -- fix FIELD_MAP and re-run export-options")
            if iter_field:
                raise SystemExit(
                    f"only one '{PATTERN_PREFIX}' pattern allowed at a time "
                    f"(found {iter_field} and {key})")

            matches = [o for o in options[key]["options"]
                       if needle in o.lower() and o.strip().lower() not in NEUTRAL]
            if not matches:
                raise SystemExit(f"{key}: no option contains {needle!r}")

            iter_field, iter_values = key, matches
            base[key] = None
        elif key == "post_isb_company" and isinstance(val, list) and val:
            if iter_field:
                raise SystemExit(
                    f"only one company loop allowed at a time "
                    f"(found {iter_field} and {key})")
            iter_field, iter_values = key, [str(v) for v in val]
            base[key] = None
        else:
            base[key] = val

    return base, iter_field, iter_values

def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape resumes from the ISB CAS-KMP portal.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export-options")
    

    sub.add_parser("login")
    sub.add_parser("inspect")

    r = sub.add_parser("run")
    r.add_argument("--years", nargs="+", type=int, required=True)
    r.add_argument(
        "--company", action="append", metavar="NAME",
        help="Post-ISB company (repeatable). Quote names with spaces. "
             "Use ~text to match several dropdown options.",
    )
    r.add_argument("--manual-search", action="store_true")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--limit", type=int)

    args = ap.parse_args()

    if args.cmd == "login":
        asyncio.run(cmd_login())
    elif args.cmd == "inspect":
        asyncio.run(cmd_inspect())
    elif args.cmd == "export-options":
        asyncio.run(cmd_export_options())
    else:
        asyncio.run(cmd_run(
            args.years, args.manual_search, args.headless, args.limit, args.company,
        ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

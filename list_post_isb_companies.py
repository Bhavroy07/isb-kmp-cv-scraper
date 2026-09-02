#!/usr/bin/env python3
"""Dump every Post-ISB company from the CAS-KMP search dropdown.

Uses the same login profile as isb_kmp_scraper.py.

    python list_post_isb_companies.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from isb_kmp_scraper import (
    FIELD_MAP,
    NEUTRAL,
    SEARCH_PAGE,
    ainput,
    log,
    looks_logged_out,
    open_context,
)

OUT_JSON = Path("post_isb_companies.json")
OUT_TXT = Path("post_isb_companies.txt")
FRAG = FIELD_MAP["post_isb_company"]

EXTRACT_JS = """
(frag) => {
  const hay = s => ((s.id || '') + ' ' + (s.name || '')).toLowerCase();
  const sel = Array.from(document.querySelectorAll('select'))
    .find(s => hay(s).includes(frag));
  if (!sel) return {ok: false, reason: 'no matching select', id: null, options: []};
  const options = Array.from(sel.options).map(o =>
    (o.text || '').replace(/\\u00a0/g, ' ').trim()
  ).filter(Boolean);
  return {ok: true, id: sel.id || sel.name, options};
}
"""


def clean(names: list[str]) -> list[str]:
    seen, out = set(), []
    for name in names:
        key = name.strip()
        if not key or key.lower() in NEUTRAL or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append(key)
    return out


async def main() -> None:
    async with async_playwright() as pw:
        context = await open_context(pw, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")
        if await looks_logged_out(page):
            await ainput("Log in, land on the search form, then press Enter... ")
            await page.goto(SEARCH_PAGE, wait_until="domcontentloaded")

        res = await page.evaluate(EXTRACT_JS, FRAG)
        await context.close()

    if not res.get("ok"):
        raise SystemExit(
            f"Could not find the Post-ISB company dropdown ({res.get('reason')}). "
            "Run `python isb_kmp_scraper.py inspect` and check form_dump.json."
        )

    companies = clean(res["options"])
    OUT_JSON.write_text(json.dumps(companies, indent=2, ensure_ascii=False) + "\n")
    OUT_TXT.write_text("\n".join(companies) + "\n")
    log(f"Found {len(companies)} companies from {res['id']}")
    log(f"  -> {OUT_JSON}")
    log(f"  -> {OUT_TXT}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)

#!/usr/bin/env python3
"""
LinkedIn MCP Diagnostic Toolkit
================================
Run this when search_linkedin_posts or browse_linkedin_feed start returning
empty results or errors. Opens a real browser and dumps the live DOM so you
can update selectors in linkedin_browser_mcp.py.

Usage (always from repo root, with DISPLAY set):
    DISPLAY=:0 .venv/bin/python3 diagnose.py search "life cycle assessment"
    DISPLAY=:0 .venv/bin/python3 diagnose.py feed
    .venv/bin/python3 diagnose.py selectors
    .venv/bin/python3 diagnose.py validate

Commands:
    search <query>   Dump DOM of search results page (default command)
    feed             Dump DOM of the feed page
    selectors        Print all CSS selectors used in linkedin_browser_mcp.py
    validate         Syntax-check all JS blocks with node
"""

import asyncio
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from cryptography.fernet import Fernet
from playwright.async_api import async_playwright

REPO = Path(__file__).parent
SESSIONS = REPO / "sessions"
MCP_FILE = REPO / "linkedin_browser_mcp.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_cookies():
    key_bytes = (SESSIONS / "encryption.key").read_bytes()
    f = Fernet(key_bytes)
    raw = (SESSIONS / "linkedin_carlos_cookies.json").read_bytes()
    data = json.loads(f.decrypt(raw))
    return data["cookies"]


async def open_page(url: str):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    await ctx.add_cookies(load_cookies())
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(10000)
    return pw, browser, page

# ---------------------------------------------------------------------------
# JS payloads (defined as Python variables to avoid triple-quote nesting)
# ---------------------------------------------------------------------------

# Injected into the search page to understand the current DOM structure.
# Returns: strategy hit counts, data-view-name inventory, componentkey samples,
# and a detailed child-element dump of the first real post container found.
SEARCH_DOM_INSPECTOR_JS = """() => {
    function getCls(el) {
        try {
            var cn = typeof el.className === 'string'
                ? el.className
                : (el.className && el.className.baseVal) || '';
            return cn.substring(0, 100);
        } catch(e) { return ''; }
    }

    // --- Strategy hit counts ---
    var strategies = {
        'data-urn[activity]':
            document.querySelectorAll('[data-urn^="urn:li:activity"]').length,
        'data-view-name=feed-full-update':
            document.querySelectorAll('[data-view-name="feed-full-update"]').length,
        'data-view-name=search-entity-result':
            document.querySelectorAll(
                '[data-view-name="search-entity-result-universal-template"]'
            ).length,
        'occludable-update':
            document.querySelectorAll('.occludable-update').length,
        '[componentkey] with profile link + text > 80': (function() {
            return Array.from(document.querySelectorAll('[componentkey]')).filter(function(el) {
                return (
                    !!el.querySelector('a[href*="/in/"], a[href*="/company/"]') &&
                    (el.innerText || '').length > 80
                );
            }).length;
        })()
    };

    // --- All unique data-view-name values on page ---
    var viewNames = Array.from(
        new Set(
            Array.from(document.querySelectorAll('[data-view-name]'))
                .map(function(el) { return el.getAttribute('data-view-name'); })
        )
    ).sort();

    // --- First 20 componentkey values ---
    var ckSamples = Array.from(document.querySelectorAll('[componentkey]'))
        .slice(0, 20)
        .map(function(el) { return el.getAttribute('componentkey'); });

    // --- First real post container: detailed child dump ---
    var candidates = Array.from(document.querySelectorAll('[componentkey]')).filter(function(el) {
        var ck = el.getAttribute('componentkey') || '';
        if (['SearchResults_', 'primaryNav', 'compact-footer', 'search-reusables']
                .some(function(p) { return ck.startsWith(p); })) return false;
        return (
            !!el.querySelector('a[href*="/in/"], a[href*="/company/"]') &&
            (el.innerText || '').length > 80
        );
    });

    var first = candidates[0] || null;
    var firstDump = null;
    if (first) {
        firstDump = {
            componentkey: first.getAttribute('componentkey'),
            text_preview: (first.innerText || '').substring(0, 500),
            children: Array.from(first.querySelectorAll(
                'a[href], time, [dir="ltr"], [aria-label], [data-view-name], [componentkey]'
            )).slice(0, 30).map(function(el) {
                return {
                    tag: el.tagName,
                    href: (el.getAttribute('href') || '').substring(0, 120),
                    aria: (el.getAttribute('aria-label') || '').substring(0, 80),
                    dir: el.getAttribute('dir') || '',
                    datetime: el.getAttribute('datetime') || '',
                    data_view_name: el.getAttribute('data-view-name') || '',
                    componentkey: (el.getAttribute('componentkey') || '').substring(0, 60),
                    text: (el.innerText || '').trim().substring(0, 80)
                };
            })
        };
    }

    return {
        url: location.href,
        total_elements: document.querySelectorAll('*').length,
        strategies: strategies,
        view_names: viewNames,
        componentkey_samples: ckSamples,
        post_candidates_count: candidates.length,
        first_post: firstDump,
        page_text_preview: document.body.innerText.substring(0, 400)
    };
}"""


# Injected into the feed page for the same structural analysis.
FEED_DOM_INSPECTOR_JS = """() => {
    var strategies = {
        'data-urn[activity]':
            document.querySelectorAll('[data-urn^="urn:li:activity"]').length,
        'feed-shared-update-v2':
            document.querySelectorAll('.feed-shared-update-v2').length,
        'data-view-name=feed-full-update':
            document.querySelectorAll('[data-view-name="feed-full-update"]').length,
        '[componentkey] with profile link + text > 80': (function() {
            return Array.from(document.querySelectorAll('[componentkey]')).filter(function(el) {
                return (
                    !!el.querySelector('a[href*="/in/"], a[href*="/company/"]') &&
                    (el.innerText || '').length > 80
                );
            }).length;
        })()
    };

    var feedViewNames = Array.from(
        new Set(
            Array.from(document.querySelectorAll('[data-view-name]'))
                .map(function(el) { return el.getAttribute('data-view-name'); })
        )
    ).filter(function(v) { return v.includes('feed'); }).sort();

    return {
        url: location.href,
        strategies: strategies,
        feed_related_view_names: feedViewNames,
        page_text_preview: document.body.innerText.substring(0, 400)
    };
}"""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_search(query: str):
    """Open search results page and dump DOM structure."""
    url = (
        "https://www.linkedin.com/search/results/content/?keywords="
        + urllib.parse.quote(query)
    )
    print(f"Opening: {url}\nWaiting 10s for page to settle...\n")
    pw, browser, page = await open_page(url)
    result = await page.evaluate(SEARCH_DOM_INSPECTOR_JS)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    await browser.close()
    await pw.stop()


async def cmd_feed():
    """Open feed page and dump DOM structure."""
    print("Opening feed...\nWaiting 10s for page to settle...\n")
    pw, browser, page = await open_page("https://www.linkedin.com/feed/")
    result = await page.evaluate(FEED_DOM_INSPECTOR_JS)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    await browser.close()
    await pw.stop()


def cmd_selectors():
    """Print all CSS selectors currently used in linkedin_browser_mcp.py."""
    source = MCP_FILE.read_text()
    found = re.findall(r'querySelectorAll?\(["\'](.+?)["\']\)', source)
    unique = sorted(set(found))
    print(f"CSS selectors in {MCP_FILE.name} ({len(unique)} unique):\n")
    for s in unique:
        print(f"  {s}")


def cmd_validate():
    """Syntax-check every page.evaluate() JS block using node."""
    source = MCP_FILE.read_text()
    # Find every evaluate( followed by a JS arrow/function literal.
    # We look for the opening """ or ''' after evaluate( and scan for the matching close.
    blocks = []
    i = 0
    while i < len(source):
        # Find next evaluate(
        pos = source.find("evaluate(", i)
        if pos == -1:
            break
        after = pos + len("evaluate(")
        # Determine triple-quote delimiter
        chunk = source[after:after + 3]
        if chunk == '"""':
            delim = '"""'
        elif chunk == "'''":
            delim = "'''"
        else:
            i = after
            continue
        js_start = after + 3
        # Find closing delimiter (first occurrence after js_start)
        close_pos = source.find(delim, js_start)
        if close_pos == -1:
            i = after
            continue
        js = source[js_start:close_pos]
        line_num = source[:pos].count("\n") + 1
        blocks.append((line_num, js))
        i = close_pos + 3

    print(f"Found {len(blocks)} evaluate() block(s) in {MCP_FILE.name}\n")
    all_ok = True
    tmp = Path("/tmp/_li_validate.js")
    for line_num, js in blocks:
        tmp.write_text(f"const _fn = {js};\nconsole.log('ok');")
        r = subprocess.run(
            ["node", str(tmp)], capture_output=True, text=True, timeout=5
        )
        ok = r.returncode == 0
        status = "OK" if ok else "FAIL"
        print(f"  Line {line_num:4d}: {status}")
        if not ok and r.stderr:
            print(f"           {r.stderr.strip()[:200]}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("All blocks valid.")
    else:
        print("ERRORS FOUND — fix before restarting the MCP server.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "search"
    arg = sys.argv[2] if len(sys.argv) > 2 else "life cycle assessment"

    if cmd == "search":
        asyncio.run(cmd_search(arg))
    elif cmd == "feed":
        asyncio.run(cmd_feed())
    elif cmd == "selectors":
        cmd_selectors()
    elif cmd == "validate":
        cmd_validate()
    else:
        print(__doc__)
        sys.exit(1)

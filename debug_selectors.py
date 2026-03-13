#!/usr/bin/env python3
"""Debug script to capture LinkedIn's current HTML structure"""

from playwright.sync_api import sync_playwright
from pathlib import Path
import json
import time

def setup_sessions_directory():
    sessions_dir = Path(__file__).parent / 'sessions'
    sessions_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
    return sessions_dir

def load_cookies(context):
    """Load cookies from encrypted file"""
    from cryptography.fernet import Fernet
    sessions_dir = Path(__file__).parent / 'sessions'
    cookie_file = sessions_dir / 'linkedin_cookies.json'
    key_file = sessions_dir / 'encryption.key'
    
    if not cookie_file.exists() or not key_file.exists():
        return False
    
    try:
        with open(key_file, 'rb') as f:
            key = f.read()
        with open(cookie_file, 'rb') as f:
            encrypted = f.read()
        
        fernet = Fernet(key)
        cookie_data = json.loads(fernet.decrypt(encrypted))
        context.add_cookies(cookie_data["cookies"])
        print("Cookies loaded successfully")
        return True
    except Exception as e:
        print(f"Failed to load cookies: {e}")
        return False

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        
        load_cookies(context)
        page = context.new_page()
        
        print("Navigating to LinkedIn feed...")
        page.goto('https://www.linkedin.com/feed/', wait_until='networkidle', timeout=30000)
        
        if 'login' in page.url:
            print("Not logged in! Please login manually...")
            page.wait_for_url('**/feed/**', timeout=120000)
        
        print("Waiting for feed to load...")
        time.sleep(5)
        
        # Save the full HTML for analysis
        html = page.content()
        output_file = Path(__file__).parent / 'linkedin_feed_dump.html'
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"Full HTML saved to: {output_file}")
        
        # Try to find post-like elements and print their structure
        print("\n=== Searching for post containers ===")
        
        selectors_to_try = [
            '.occludable-update',
            '.feed-shared-update-v2',
            '[data-urn]',
            '[data-id]',
            '.update-components-actor',
            '.feed-shared-actor',
            'div[class*="feed"]',
            'div[class*="update"]',
            'article',
            'main > div > div',
        ]
        
        for selector in selectors_to_try:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    print(f"\n✓ Found {len(elements)} elements with: {selector}")
                    # Print first element's outer HTML (truncated)
                    if elements:
                        first_html = elements[0].evaluate('el => el.outerHTML')
                        print(f"  First element preview: {first_html[:300]}...")
            except Exception as e:
                print(f"✗ Selector '{selector}' failed: {e}")
        
        # Also dump all class names found in main content area
        print("\n=== Unique class names in main content ===")
        classes = page.evaluate('''() => {
            const main = document.querySelector('main') || document.body;
            const allClasses = new Set();
            main.querySelectorAll('*').forEach(el => {
                el.classList.forEach(c => {
                    if (c.includes('feed') || c.includes('update') || c.includes('post') || c.includes('actor')) {
                        allClasses.add(c);
                    }
                });
            });
            return Array.from(allClasses).sort();
        }''')
        
        for cls in classes[:50]:  # Print first 50
            print(f"  .{cls}")
        
        print("\n=== Done! Check linkedin_feed_dump.html for full structure ===")
        
        browser.close()

if __name__ == "__main__":
    main()

import pytest
import os
from linkedin_browser_mcp import (
    BrowserSession,
    save_cookies,
    load_cookies,
    login_linkedin,
    login_linkedin_secure,
    browse_linkedin_feed,
    search_linkedin_profiles,
    view_linkedin_profile,
    interact_with_linkedin_post
)

class MockContext:
    def info(self, message):
        print(f"INFO: {message}")
        
    def error(self, message):
        print(f"ERROR: {message}")
        
    async def report_progress(self, current, total):
        print(f"Progress: {current}/{total}")

@pytest.mark.asyncio
async def test_browser_session():
    async with BrowserSession(platform='linkedin', headless=True) as session:
        page = await session.new_page()
        assert page is not None
        assert session.browser is not None
        assert session.context is not None

@pytest.mark.asyncio
async def test_login_linkedin_secure_missing_credentials():
    ctx = MockContext()
    # Clear environment variables
    if 'LINKEDIN_USERNAME' in os.environ:
        del os.environ['LINKEDIN_USERNAME']
    if 'LINKEDIN_PASSWORD' in os.environ:
        del os.environ['LINKEDIN_PASSWORD']
    result = await login_linkedin_secure(ctx)
    assert result["status"] == "error"
    assert "Missing LinkedIn credentials" in result["message"]

@pytest.mark.asyncio
async def test_login_linkedin_secure_invalid_email():
    ctx = MockContext()
    os.environ['LINKEDIN_USERNAME'] = 'invalid-email'
    os.environ['LINKEDIN_PASSWORD'] = 'password123'
    result = await login_linkedin_secure(ctx)
    assert result["status"] == "error"
    assert "Invalid email format" in result["message"]

@pytest.mark.asyncio
async def test_login_linkedin_secure_short_password():
    ctx = MockContext()
    os.environ['LINKEDIN_USERNAME'] = 'test@example.com'
    os.environ['LINKEDIN_PASSWORD'] = 'short'
    result = await login_linkedin_secure(ctx)
    assert result["status"] == "error"
    assert "password must be at least 8 characters" in result["message"]

@pytest.mark.asyncio
async def test_view_linkedin_profile_invalid_url():
    ctx = MockContext()
    result = await view_linkedin_profile("https://invalid-url.com", ctx)
    assert result["status"] == "error"
    assert "Invalid LinkedIn profile URL" in result["message"]

@pytest.mark.asyncio
async def test_interact_with_linkedin_post_invalid_url():
    ctx = MockContext()
    result = await interact_with_linkedin_post("https://invalid-url.com", ctx)
    assert result["status"] == "error"
    assert "Invalid LinkedIn post URL" in result["message"]

@pytest.mark.asyncio
async def test_interact_with_linkedin_post_invalid_action():
    ctx = MockContext()
    result = await interact_with_linkedin_post(
        "https://linkedin.com/posts/123",
        ctx,
        action="invalid"
    )
    assert result["status"] == "error"
    assert "Invalid action" in result["message"] 
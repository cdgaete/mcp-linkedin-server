from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.server import Middleware
from mcp.types import TextContent
from playwright.async_api import async_playwright
import asyncio
import os
import json
from typing import Literal, Optional
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import time
import logging
import sys
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
import socket
import urllib.request
import urllib.parse
import aiosqlite
from aiohttp import web

# Set up logging — file handler ensures logs persist regardless of how the process is started
_log_fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_file_handler = logging.FileHandler('/tmp/linkedin-mcp.log')
_file_handler.setFormatter(_log_fmt)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_stderr_handler, _file_handler])
logger = logging.getLogger(__name__)

# Load environment variables
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)
    logger.info(f"Loaded environment from {env_path}")

# ---------------------------------------------------------------------------
# Bearer token auth for FastMCP v3
# ---------------------------------------------------------------------------
class BearerAuth(TokenVerifier):
    """Simple static bearer token verifier."""
    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token == self._token:
            return AccessToken(token=token, client_id="linkedin-mcp", scopes=[])
        return None

# Read bearer token from --http arg (legacy compat) or MCP_BEARER_TOKEN env
_bearer_token = os.getenv("MCP_BEARER_TOKEN", "")
if "--http" in sys.argv:
    _idx = sys.argv.index("--http")
    if _idx + 1 < len(sys.argv):
        _bearer_token = sys.argv[_idx + 1]

_auth = BearerAuth(_bearer_token) if _bearer_token else None

# Create FastMCP server (v3 — streamable HTTP, no restart needed on code changes)
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(server):
    """Start the auth webhook server and audit DB alongside the MCP server."""
    global _audit_db
    _audit_db = await _init_audit_db()
    logger.info(f"Audit DB initialized at {AUDIT_DB_PATH}")
    await start_webhook_server()
    yield
    if _audit_db:
        await _audit_db.close()
        _audit_db = None

mcp = FastMCP("linkedin-browser", lifespan=lifespan, auth=_auth)

# --- Async task store for long-running search operations ---
import uuid as _uuid
_search_tasks: dict = {}  # task_id -> {"status": "pending"|"done"|"error", "result": ...}


# ---------------------------------------------------------------------------
# Audit database — records every tool call for traceability
# ---------------------------------------------------------------------------
AUDIT_DB_PATH = Path(__file__).parent / "data" / "audit.db"

# Module-level connection holder (set during lifespan)
_audit_db: aiosqlite.Connection | None = None

# Session → clientInfo mapping (populated during initialize handshake)
_session_client_info: dict[str, dict] = {}


async def _init_audit_db() -> aiosqlite.Connection:
    """Create audit DB and table if needed, return connection."""
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(AUDIT_DB_PATH))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            tool_name   TEXT    NOT NULL,
            parameters  TEXT,
            response    TEXT,
            duration_ms INTEGER,
            status      TEXT    NOT NULL DEFAULT 'success',
            error_message TEXT,
            caller_id   TEXT,
            client_name TEXT,
            client_version TEXT,
            session_id  TEXT,
            ip_address  TEXT
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp
        ON tool_calls(timestamp DESC)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name
        ON tool_calls(tool_name)
    """)
    await db.commit()
    return db


async def _record_tool_call(
    tool_name: str,
    parameters: dict | None,
    response: str | None,
    duration_ms: int,
    status: str,
    error_message: str | None,
    caller_id: str | None,
    client_name: str | None,
    client_version: str | None,
    session_id: str | None,
    ip_address: str | None,
) -> None:
    """Insert a tool call record into the audit DB."""
    if _audit_db is None:
        logger.warning("Audit DB not initialized, skipping record")
        return
    try:
        await _audit_db.execute(
            """INSERT INTO tool_calls
               (timestamp, tool_name, parameters, response, duration_ms,
                status, error_message, caller_id, client_name, client_version,
                session_id, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat() + "Z",
                tool_name,
                json.dumps(parameters) if parameters else None,
                response[:10000] if response else None,  # cap response size
                duration_ms,
                status,
                error_message,
                caller_id,
                client_name,
                client_version,
                session_id,
                ip_address,
            ),
        )
        await _audit_db.commit()
    except Exception as e:
        logger.error(f"Failed to record audit: {e}")


class AuditMiddleware(Middleware):
    """Logs every tool call with timing, caller info, and response."""

    async def on_initialize(self, context, call_next):
        """Capture clientInfo from the initialize handshake."""
        result = await call_next(context)
        # Store client info keyed by session once context is available
        try:
            ctx = context.fastmcp_context
            if ctx:
                sid = ctx.session_id
                params = context.message
                client_info = getattr(params, "clientInfo", None)
                if client_info:
                    _session_client_info[sid] = {
                        "name": getattr(client_info, "name", None),
                        "version": getattr(client_info, "version", None),
                    }
                    logger.info(f"Audit: session {sid[:12]}… client={client_info.name}/{client_info.version}")
        except Exception as e:
            logger.debug(f"Audit: could not capture clientInfo: {e}")
        return result

    async def on_call_tool(self, context, call_next):
        """Wrap every tool call with timing and audit logging."""
        tool_name = context.message.name
        parameters = context.message.arguments
        ctx = context.fastmcp_context

        # Gather identity
        session_id = None
        caller_id = None
        client_name = None
        client_version = None
        ip_address = None

        try:
            if ctx:
                session_id = ctx.session_id
                caller_id = ctx.client_id
                info = _session_client_info.get(session_id, {})
                client_name = info.get("name")
                client_version = info.get("version")
        except Exception:
            pass

        try:
            from fastmcp.server.dependencies import get_http_request
            req = get_http_request()
            # Starlette Request — check X-Forwarded-For (Cloudflare) then client
            ip_address = (
                req.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or req.headers.get("cf-connecting-ip")
                or (req.client.host if req.client else None)
            )
        except Exception:
            pass

        t0 = time.monotonic()
        status = "success"
        error_message = None
        response_text = None

        try:
            result = await call_next(context)
            # result is a list of TextContent / ImageContent etc.
            if result:
                texts = [getattr(c, "text", "") for c in result if hasattr(c, "text")]
                response_text = "\n".join(texts)
            return result
        except Exception as exc:
            status = "error"
            error_message = str(exc)
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            asyncio.create_task(_record_tool_call(
                tool_name=tool_name,
                parameters=parameters,
                response=response_text,
                duration_ms=duration_ms,
                status=status,
                error_message=error_message,
                caller_id=caller_id,
                client_name=client_name,
                client_version=client_version,
                session_id=session_id,
                ip_address=ip_address,
            ))

mcp.add_middleware(AuditMiddleware())


def parse_linkedin_date(date_str: str) -> tuple[datetime, int]:
    """
    Parse LinkedIn relative date string into actual date and age in days.
    
    LinkedIn formats: "1h", "2h", "1d", "5d", "1w", "2w", "1mo", "2mo", "1yr"
    Also handles: "Just now", "5 hours ago", "3 days ago", etc.
    LinkedIn often adds " • " or " • Edited" after the date.
    
    Returns: (parsed_datetime, age_in_days)
    """
    if not date_str:
        return datetime.now(), 0
    
    # Clean up the string - remove bullets, dots, 'Edited', extra spaces
    date_str = date_str.lower().strip()
    date_str = re.sub(r'[•·].*', '', date_str)  # Remove everything after bullet
    date_str = re.sub(r'edited.*', '', date_str, flags=re.IGNORECASE)
    date_str = date_str.strip()
    
    now = datetime.now()
    
    # Handle "just now" or very recent
    if 'just now' in date_str or 'now' in date_str:
        return now, 0
    
    # Pattern matching for LinkedIn formats
    # Short format: 1h, 2d, 1w, 1mo, 1yr
    short_match = re.match(r'^(\d+)(h|d|w|mo|yr)$', date_str)
    if short_match:
        value = int(short_match.group(1))
        unit = short_match.group(2)
        
        if unit == 'h':
            delta = timedelta(hours=value)
            days = 0
        elif unit == 'd':
            delta = timedelta(days=value)
            days = value
        elif unit == 'w':
            delta = timedelta(weeks=value)
            days = value * 7
        elif unit == 'mo':
            delta = timedelta(days=value * 30)
            days = value * 30
        elif unit == 'yr':
            delta = timedelta(days=value * 365)
            days = value * 365
        else:
            return now, 0
        
        return now - delta, days
    
    # Long format: "5 hours ago", "3 days ago", "2 weeks ago"
    long_match = re.match(r'^(\d+)\s*(hour|day|week|month|year)s?\s*(?:ago)?$', date_str)
    if long_match:
        value = int(long_match.group(1))
        unit = long_match.group(2)
        
        if unit == 'hour':
            delta = timedelta(hours=value)
            days = 0
        elif unit == 'day':
            delta = timedelta(days=value)
            days = value
        elif unit == 'week':
            delta = timedelta(weeks=value)
            days = value * 7
        elif unit == 'month':
            delta = timedelta(days=value * 30)
            days = value * 30
        elif unit == 'year':
            delta = timedelta(days=value * 365)
            days = value * 365
        else:
            return now, 0
        
        return now - delta, days
    
    # If we can't parse, assume it's recent (today)
    return now, 0


def filter_posts_by_age(posts: list, max_age_days: int) -> list:
    """Filter posts to only include those within max_age_days."""
    if max_age_days <= 0:
        return posts
    
    filtered = []
    for post in posts:
        date_str = post.get('date', '')
        parsed_date, age_days = parse_linkedin_date(date_str)
        
        # Add parsed date info to post
        post['parsed_date'] = parsed_date.strftime('%Y-%m-%d')
        post['age_days'] = age_days
        
        if age_days <= max_age_days:
            filtered.append(post)
    
    return filtered


def setup_sessions_directory():
    sessions_dir = Path(__file__).parent / 'sessions'
    sessions_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
    return sessions_dir


# Account configurations
LINKEDIN_ACCOUNTS = {
    "carlos": {
        "description": "Carlos - EcoSemantic admin, expert LCA comments",
        "company_id": os.getenv("LINKEDIN_CARLOS_COMPANY_ID"),  # EcoSemantic showcase page ID
    },
    "claudia": {
        "description": "Claudia - Personal profile, groups, networking",
        "company_id": None,  # Posts as personal
    }
}

# Current active account (can be changed via login)
CURRENT_ACCOUNT = os.getenv("LINKEDIN_ACCOUNT", "carlos")


def get_cookie_filename(account: str = None) -> str:
    """Get cookie filename for an account."""
    acc = account or CURRENT_ACCOUNT
    return f'linkedin_{acc}_cookies.json'


def require_session(account: str = None) -> list | None:
    """Check if a valid cookie file exists. If not, fire Telegram and return an error TextContent list.
    Call at the top of every tool handler before launching any browser."""
    acc = account or CURRENT_ACCOUNT
    sessions_dir = Path(__file__).parent / 'sessions'
    cookie_file = sessions_dir / get_cookie_filename(acc)
    if not cookie_file.exists():
        notify_login_required(acc)
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "message": f"Not logged in as {acc}. A login request has been sent via Telegram."
        }))]
    return None


async def save_cookies(page, account: str = None, cdp_port: int = None):
    """Save cookies to encrypted file for specific account.
    
    If page belongs to a BrowserSession, uses that session's account.
    cdp_port: if provided, use this port directly for CDP Network.getAllCookies
              instead of discovering via ps aux (avoids wrong-process issues).
    """
    # Try to get account from browser context if not specified
    if account is None:
        # Check if we stored account in context
        acc = getattr(page.context, '_account', None) or CURRENT_ACCOUNT
    else:
        acc = account
    
    # Use CDP Network.getAllCookies to capture httpOnly cookies (Playwright context.cookies() misses them)
    import urllib.request as _req
    cookies = None
    if cdp_port:
        try:
            import websockets as _ws
            async def _cdp_cookies():
                tabs = json.loads(_req.urlopen(f"http://localhost:{cdp_port}/json").read())
                # Find the LinkedIn tab, not just tabs[0]
                li_tab = next((t for t in tabs if 'linkedin.com' in t.get('url', '')), tabs[0])
                ws_url = li_tab['webSocketDebuggerUrl']
                async with _ws.connect(ws_url) as ws:
                    await ws.send(json.dumps({'id': 1, 'method': 'Network.getAllCookies'}))
                    resp = json.loads(await ws.recv())
                    all_c = resp['result']['cookies']
                    return [c for c in all_c if 'linkedin' in c.get('domain', '')]
            cookies = await _cdp_cookies()
            logger.info(f"Captured {len(cookies)} cookies via CDP (port {cdp_port}) for {acc}")
        except Exception as e:
            logger.warning(f"CDP cookie capture failed ({e}), falling back to Playwright")
            cookies = None
    if cookies is None:
        cookies = await page.context.cookies()
        logger.info(f"Captured {len(cookies)} cookies via Playwright for {acc}")
    cookie_data = {"timestamp": int(time.time()), "cookies": cookies, "account": acc}

    sessions_dir = setup_sessions_directory()

    key_file = sessions_dir / 'encryption.key'
    if key_file.exists():
        with open(key_file, 'rb') as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(key_file, 'wb') as f:
            f.write(key)

    fernet = Fernet(key)
    encrypted = fernet.encrypt(json.dumps(cookie_data).encode())

    cookie_file = sessions_dir / get_cookie_filename(acc)
    with open(cookie_file, 'wb') as f:
        f.write(encrypted)
    logger.info(f"Cookies saved for account: {acc}")


async def load_cookies(context, account: str = None):
    """Load cookies from encrypted file for specific account"""
    acc = account or CURRENT_ACCOUNT
    sessions_dir = Path(__file__).parent / 'sessions'
    cookie_file = sessions_dir / get_cookie_filename(acc)
    key_file = sessions_dir / 'encryption.key'

    if not cookie_file.exists() or not key_file.exists():
        logger.warning(f"No cookies found for account: {acc}")
        return False

    try:
        with open(key_file, 'rb') as f:
            key = f.read()
        with open(cookie_file, 'rb') as f:
            encrypted = f.read()

        fernet = Fernet(key)
        cookie_data = json.loads(fernet.decrypt(encrypted))

        # Check expiration (24 hours)
        if time.time() - cookie_data["timestamp"] > 86400:
            cookie_file.unlink()
            return False

        await context.add_cookies(cookie_data["cookies"])
        logger.info(f"Cookies loaded for account: {acc}")
        return True
    except Exception as e:
        logger.warning(f"Failed to load cookies for {acc}: {e}")
        return False


class BrowserSession:
    def __init__(self, headless=True, account: str = None):
        self.headless = headless
        self.account = account or CURRENT_ACCOUNT
        self.playwright = None
        self.browser = None
        self.context = None

    async def __aenter__(self):
        setup_sessions_directory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=['--disable-dev-shm-usage', '--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        self.context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        # Store account in context for save_cookies to access
        self.context._account = self.account
        await load_cookies(self.context, self.account)
        return self

    async def __aexit__(self, *args):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def new_page(self, url=None):
        page = await self.context.new_page()
        if url:
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        return page


# ---------------------------------------------------------------------------
# FastMCP tool definitions
# Each @mcp.tool() wraps the existing do_* handlers.
# do_* functions return [TextContent(...)]; we extract .text for FastMCP.
# ---------------------------------------------------------------------------

def _extract(result: list) -> str:
    """Extract text from legacy [TextContent(...)] return values."""
    return result[0].text if result else "{}"


@mcp.tool()
async def login_linkedin(account: Literal["carlos", "claudia"] = "carlos") -> str:
    """Step 1 of 2: Open LinkedIn login page in a visible browser window. Returns immediately — do NOT wait. After finishing manual login, call login_linkedin_save to capture the session."""
    return _extract(await do_login_start(account))


@mcp.tool()
async def login_linkedin_save(account: Literal["carlos", "claudia"] = "carlos") -> str:
    """Step 2 of 2: Call this after finishing manual login in the browser opened by login_linkedin. Saves session cookies and closes the browser."""
    return _extract(await do_login_save(account))


@mcp.tool()
async def browse_linkedin_feed(count: int = 5, max_age_days: int = 0) -> str:
    """Browse LinkedIn feed and return recent posts. Use max_age_days to filter old posts."""
    return _extract(await do_browse_feed(count, max_age_days))


@mcp.tool()
async def search_linkedin_profiles(query: str, count: int = 5) -> str:
    """Search for LinkedIn profiles matching a query."""
    return _extract(await do_search_profiles(query, count))


@mcp.tool()
async def search_linkedin_posts(
    query: str,
    count: int = 5,
    max_age_days: int = 0,
    date_posted: Optional[Literal["past-24h", "past-week", "past-month"]] = None,
    sort_by: Optional[Literal["relevance", "date_posted"]] = None,
    author_name: Optional[str] = None,
    account: Literal["carlos", "claudia"] = "carlos",
) -> str:
    """Search for LinkedIn posts by keywords. Supports filters:
    - date_posted: 'past-24h', 'past-week', 'past-month' (LinkedIn native filter)
    - sort_by: 'relevance' (default) or 'date_posted' (latest first)
    - author_name: client-side filter by author name (partial, case-insensitive)
    - max_age_days: client-side age filter (legacy, prefer date_posted)
    Note: post URLs are not available from search results; use get_linkedin_posts on the author's profile to find specific post URLs."""
    return _extract(await do_search_posts(query, count, max_age_days, date_posted, sort_by, author_name, account))


@mcp.tool()
async def search_linkedin_posts_async(
    query: str,
    count: int = 5,
    max_age_days: int = 0,
    date_posted: Optional[Literal["past-24h", "past-week", "past-month"]] = None,
    sort_by: Optional[Literal["relevance", "date_posted"]] = None,
    author_name: Optional[str] = None,
    account: Literal["carlos", "claudia"] = "carlos",
) -> str:
    """Start a LinkedIn post search in the background. Returns a task_id immediately. Poll get_search_task_status with the task_id to retrieve results when done (~15s). Supports same filters as search_linkedin_posts."""
    task_id = str(_uuid.uuid4())
    _search_tasks[task_id] = {"status": "pending", "result": None}

    async def _run_search(tid, q, c, m, dp, sb, an, acc):
        try:
            result = await do_search_posts(q, c, m, dp, sb, an, acc)
            _search_tasks[tid] = {"status": "done", "result": json.loads(result[0].text)}
        except Exception as e:
            _search_tasks[tid] = {"status": "error", "result": {"message": str(e)}}

    asyncio.create_task(_run_search(task_id, query, count, max_age_days, date_posted, sort_by, author_name, account))
    return json.dumps({"task_id": task_id, "status": "pending"})


@mcp.tool()
async def get_search_task_status(task_id: str) -> str:
    """Poll the status of a search_linkedin_posts_async task. Returns status=pending while running, status=done with full posts list when complete, or status=error."""
    task = _search_tasks.get(task_id)
    if not task:
        return json.dumps({"status": "error", "message": f"Unknown task_id: {task_id}"})
    return json.dumps({"task_id": task_id, **task})


@mcp.tool()
async def view_linkedin_profile(profile_url: str) -> str:
    """Visit and extract data from a LinkedIn profile URL."""
    return _extract(await do_view_profile(profile_url))


@mcp.tool()
async def get_linkedin_posts(profile_url: str, count: int = 5) -> str:
    """Get posts from a LinkedIn profile or company page with their URLs and URNs. Pass a full URL, a path like 'company/ecosemantic' or 'in/carlosgaete', or just a slug like 'ecosemantic'."""
    return _extract(await do_get_posts(profile_url, count))


@mcp.tool()
async def interact_with_linkedin_post(
    post_url: str,
    action: Literal["like", "comment", "read"] = "read",
    comment: Optional[str] = None,
    account: Literal["carlos", "claudia"] = "carlos",
    company_id: Optional[str] = None,
) -> str:
    """Interact with a LinkedIn post (like, comment, or read). Use 'account' to select which LinkedIn account (carlos or claudia). Use 'company_id' to like/comment as a company page (auto-set for carlos = EcoSemantic). Pass 'personal' to force acting as the personal account instead of company."""
    # "personal" is a sentinel to force personal identity (no company)
    resolved_company = company_id
    if resolved_company and resolved_company.lower() == "personal":
        resolved_company = None
    # Auto-set company_id for carlos only if not explicitly provided
    elif account == "carlos" and resolved_company is None:
        resolved_company = LINKEDIN_ACCOUNTS["carlos"]["company_id"]

    return _extract(await do_interact_post(post_url, action, comment, resolved_company, account))



@mcp.tool()
async def create_linkedin_post(
    content: str,
    account: Literal["carlos", "claudia"] = "carlos",
    company_id: Optional[str] = None,
    group_name: Optional[str] = None,
) -> str:
    """Publish a new LinkedIn post. Use 'account' to select which LinkedIn account. Use 'company_id' to post as a company page (auto-set for carlos = EcoSemantic). Pass 'personal' to force posting as the personal account. Use 'group_name' to post to a LinkedIn group (partial match, case-insensitive). Group posts are always as the personal account. Use list_linkedin_groups to see available groups."""
    if group_name:
        # Group posts are always personal — ignore company_id
        return _extract(await do_create_post(content, None, account, group_name=group_name))
    resolved_company = company_id
    if resolved_company and resolved_company.lower() == "personal":
        resolved_company = None
    elif account == "carlos" and resolved_company is None:
        resolved_company = LINKEDIN_ACCOUNTS["carlos"]["company_id"]
    return _extract(await do_create_post(content, resolved_company, account))


@mcp.tool()
async def delete_linkedin_post(
    post_url: str,
    account: Literal["carlos", "claudia"] = "carlos",
) -> str:
    """Delete a LinkedIn post by its URL. Works for both personal and company posts. The post URL should be in the format https://www.linkedin.com/feed/update/urn:li:activity:... or https://www.linkedin.com/posts/..."""
    return _extract(await do_delete_post(post_url, account))


@mcp.tool()
async def list_linkedin_groups(account: Literal["carlos", "claudia"] = "carlos") -> str:
    """List all LinkedIn groups the account is a member of. Returns group names, URNs, and public/private status."""
    return _extract(await do_list_groups(account))


# ---------------------------------------------------------------------------
# Auth webhook server
# Runs on AUTH_WEBHOOK_PORT alongside the MCP stdio server.
# Endpoints:
#   GET  /status          — current login state
#   GET  /login?account=  — open browser (same as do_login_start)
#   GET  /save?account=   — save cookies (same as do_login_save)
# Exposed via auth.kimel.tech through Cloudflare Tunnel.
# ---------------------------------------------------------------------------

AUTH_WEBHOOK_PORT  = int(os.getenv("AUTH_WEBHOOK_PORT", "8765"))
AUTH_BASE_URL      = os.getenv("AUTH_BASE_URL", "http://localhost:8765")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram(text: str, reply_markup: dict = None):
    """Send a Telegram message. Silently skips if bot token not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram notification sent")
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")


def notify_login_required(account: str):
    """Send Telegram message with auto-fill and save buttons."""
    fill_url = f"{AUTH_BASE_URL}/login-fill?account={account}"
    save_url = f"{AUTH_BASE_URL}/save?account={account}"
    vnc_url  = os.getenv("VNC_URL", "")
    text = (
        f"🔐 <b>LinkedIn login required</b>\n"
        f"Account: <code>{account}</code>\n\n"
        f"1. Tap <b>Auto Login</b> — fills credentials automatically\n"
        f"2. Approve the confirmation LinkedIn sends to your phone\n"
        f"3. Tap <b>Save Session</b> — cookies saved, agent continues\n\n"
        f"<i>If CAPTCHA appears, use VNC to complete manually</i>"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🤖 Auto Login", "url": fill_url},
                {"text": "✅ Save Session", "url": save_url},
            ],
            [
                {"text": "🖥️ VNC (manual fallback)", "url": vnc_url},
            ]
        ]
    }
    send_telegram(text, keyboard)


async def webhook_status(request: web.Request) -> web.Response:
    accounts = {}
    for acc in LINKEDIN_ACCOUNTS:
        pf = _login_port_file(acc)
        accounts[acc] = {"waiting_for_save": pf.exists(), "port": int(pf.read_text()) if pf.exists() else None}
    return web.json_response({"status": "ok", "accounts": accounts})


async def webhook_login_fill(request: web.Request) -> web.Response:
    """Read credentials from .env, fill login form via CDP, save cookies automatically."""
    account = request.rel_url.query.get("account", "carlos")
    key_email = f"LINKEDIN_{account.upper()}_EMAIL"
    key_pass  = f"LINKEDIN_{account.upper()}_PASSWORD"
    email    = os.environ.get(key_email)
    password = os.environ.get(key_pass)

    if not email or not password:
        return _html_response({"status": "error",
            "message": f"No credentials found for '{account}'. Set {key_email} and {key_pass} in .env"})

    # Find the open CDP browser for this account
    pf = _login_port_file(account)
    if not pf.exists():
        raw = await do_login_start(account)
        # do_login_start returns [TextContent(...)], unwrap it
        result = json.loads(raw[0].text) if isinstance(raw, list) else raw
        if result.get("status") == "success":
            return _html_response({"status": "success", "message": "Already logged in."})
        cdp_port = result.get("cdp_port")
        await asyncio.sleep(3)  # wait for browser to fully launch
    else:
        cdp_port = int(pf.read_text())

    try:
        from playwright.async_api import async_playwright
        import urllib.request as urlreq
        import json as _json

        # Verify the port is actually alive; if not, clear stale file and relaunch
        try:
            urlreq.urlopen(f"http://localhost:{cdp_port}/json", timeout=2).read()
        except Exception:
            pf.unlink(missing_ok=True)
            raw = await do_login_start(account)
            result = json.loads(raw[0].text) if isinstance(raw, list) else raw
            if result.get("status") == "success":
                return _html_response({"status": "success", "message": "Already logged in."})
            cdp_port = result.get("cdp_port")
            await asyncio.sleep(3)

        # Get tabs via CDP HTTP
        tabs = _json.loads(urlreq.urlopen(f"http://localhost:{cdp_port}/json").read())
        login_tab = next((t for t in tabs if "linkedin.com/login" in t.get("url", "")), None)

        if not login_tab:
            # Already past login — just save
            pass
        else:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
                for ctx in browser.contexts:
                    for page in ctx.pages:
                        if "linkedin.com/login" in page.url:
                            await page.fill('#username', email)
                            await asyncio.sleep(0.5)
                            await page.fill('#password', password)
                            await asyncio.sleep(0.5)
                            await page.click('[data-litms-control-urn="login-submit"]')
                            await asyncio.sleep(4)
                            # If still on login page (not checkpoint/verification), something went wrong
                            if "linkedin.com/login" in page.url:
                                return _html_response({"status": "error",
                                    "message": "Login failed — wrong credentials or CAPTCHA. Use VNC to complete manually."})
                            # checkpoint/verification = LinkedIn is waiting for phone approval — that's expected
                            if "checkpoint" in page.url or "verification" in page.url or "challenge" in page.url:
                                return _html_response({"status": "waiting",
                                    "message": "✅ Credentials accepted. Approve the confirmation on your phone, then tap Save Session."})

        # Save cookies via CDP websocket
        raw_save = await do_login_save(account)
        result_save = json.loads(raw_save[0].text) if isinstance(raw_save, list) else raw_save
        return _html_response(result_save)

    except Exception as e:
        logger.error(f"login_fill error: {e}")
        return _html_response({"status": "error", "message": str(e)})


def _html_response(data: dict) -> web.Response:
    status  = data.get("status", "")
    message = data.get("message", "")
    if status == "success":
        emoji, color = "✅", "#2d6a4f"
    elif status == "waiting":
        emoji, color = "⏳", "#b5620a"
    elif status == "error":
        emoji, color = "❌", "#a4262c"
    else:
        emoji, color = "ℹ️", "#333"
    html = f"""<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LinkedIn MCP</title>
<style>
  body{{font-family:-apple-system,sans-serif;display:flex;align-items:center;
        justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}}
  .card{{background:#fff;border-radius:16px;padding:32px 28px;max-width:360px;
         width:90%;box-shadow:0 4px 24px rgba(0,0,0,.1);text-align:center}}
  .emoji{{font-size:52px;margin-bottom:12px}}
  .status{{font-size:13px;font-weight:700;text-transform:uppercase;
           color:{color};letter-spacing:.08em;margin-bottom:8px}}
  .message{{font-size:16px;color:#333;line-height:1.5}}
  .account{{display:inline-block;margin-top:16px;background:#eef;
            color:#336;padding:4px 12px;border-radius:20px;font-size:13px}}
</style></head><body><div class="card">
<div class="emoji">{emoji}</div>
<div class="status">{status}</div>
<div class="message">{message}</div>
{f'<div class="account">@{data["account"]}</div>' if "account" in data else ""}
</div></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def webhook_login(request: web.Request) -> web.Response:
    account = request.rel_url.query.get("account", "carlos")
    result = await do_login_start(account)
    data = json.loads(result[0].text)
    return _html_response(data)


async def webhook_save(request: web.Request) -> web.Response:
    account = request.rel_url.query.get("account", "carlos")
    result = await do_login_save(account)
    data = json.loads(result[0].text)
    return _html_response(data)


async def webhook_audit(request: web.Request) -> web.Response:
    """Query audit log. Params: tool, client, session, last (int), since (ISO datetime)."""
    if _audit_db is None:
        return web.json_response({"error": "Audit DB not initialized"}, status=503)

    conditions = []
    params = []

    tool = request.rel_url.query.get("tool")
    if tool:
        conditions.append("tool_name = ?")
        params.append(tool)

    client = request.rel_url.query.get("client")
    if client:
        conditions.append("client_name = ?")
        params.append(client)

    session = request.rel_url.query.get("session")
    if session:
        conditions.append("session_id = ?")
        params.append(session)

    since = request.rel_url.query.get("since")
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)

    status_filter = request.rel_url.query.get("status")
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)

    limit = min(int(request.rel_url.query.get("last", "50")), 500)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT * FROM tool_calls{where} ORDER BY id DESC LIMIT ?"
    params.append(limit)

    try:
        cursor = await _audit_db.execute(query, params)
        columns = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        records = [dict(zip(columns, row)) for row in rows]
        return web.json_response({"count": len(records), "records": records})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def start_webhook_server():
    app = web.Application()
    app.router.add_get("/status", webhook_status)
    app.router.add_get("/login",  webhook_login)
    app.router.add_get("/login-fill", webhook_login_fill)
    app.router.add_get("/save",   webhook_save)
    app.router.add_get("/audit",  webhook_audit)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", AUTH_WEBHOOK_PORT)
    await site.start()
    logger.info(f"Auth webhook listening on port {AUTH_WEBHOOK_PORT}")





# Temp file that records the CDP debugging port for the login browser.
# Survives MCP server restarts so login_save can reconnect via CDP.
def _login_port_file(account: str) -> Path:
    return Path("/tmp") / f"linkedin_login_{account}.port"


async def do_login_start(account: str = "carlos"):
    """Step 1: Launch Chromium with remote-debugging-port, return immediately."""
    import subprocess, socket, time as _time

    if account not in LINKEDIN_ACCOUNTS:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Unknown account: {account}"}))]

    # Kill any leftover login browser for this account
    port_file = _login_port_file(account)
    if port_file.exists():
        try:
            old_port = int(port_file.read_text().strip())
            # Try to connect and close gracefully via CDP
            pw0 = await async_playwright().start()
            try:
                b0 = await pw0.chromium.connect_over_cdp(f"http://localhost:{old_port}")
                await b0.close()
            except Exception:
                pass
            await pw0.stop()
        except Exception:
            pass
        port_file.unlink(missing_ok=True)

    # Find a free port
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockename()[1] if hasattr(s, "getsockename") else s.getsockname()[1]

    # Load existing cookies to pre-populate the session
    sessions_dir = setup_sessions_directory()
    cookie_args = []

    # Launch Chromium as a subprocess with CDP enabled (survives MCP restarts)
    chromium_bin = None
    for candidate in [
        Path.home() / ".local/share/ms-playwright/chromium-*/chrome-linux/chrome",
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]:
        matches = list(Path("/").glob(str(candidate).lstrip("/"))) if "*" in str(candidate) else ([candidate] if candidate.exists() else [])
        if matches:
            chromium_bin = str(matches[0])
            break

    if not chromium_bin:
        # Fall back to playwright-managed launch without CDP persistence
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                  f"--remote-debugging-port={port}"]
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        ctx._account = account
        await load_cookies(ctx, account)
        page = await ctx.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        port_file.write_text(str(port))
        if "feed" in page.url:
            await save_cookies(page, account)
            await browser.close()
            await pw.stop()
            port_file.unlink(missing_ok=True)
            return [TextContent(type="text", text=json.dumps({"status": "success", "message": f"Already logged in as {account}. Cookies saved.", "account": account}))]
        # Keep pw/browser alive — they will be garbage-collected only when the process exits.
        # login_save will reconnect via CDP port.
        return [TextContent(type="text", text=json.dumps({
            "status": "waiting",
            "message": f"Browser open on CDP port {port}. Log in manually, then call login_linkedin_save.",
            "account": account, "cdp_port": port, "next_step": "login_linkedin_save"
        }))]

    # Launch as detached subprocess so it survives MCP restarts
    proc = subprocess.Popen(
        [chromium_bin, f"--remote-debugging-port={port}", "--no-sandbox",
         "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
         "--user-data-dir=/tmp/linkedin_login_profile_" + account,
         "https://www.linkedin.com/login"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    port_file.write_text(f"{port}:{proc.pid}")
    _time.sleep(2)  # Let Chromium start

    # Load cookies into the new browser via CDP
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        await load_cookies(ctx, account)
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()
        await page.reload(wait_until="domcontentloaded")
        if "feed" in page.url:
            await save_cookies(page, account)
            await browser.close()
            await pw.stop()
            proc.terminate()
            port_file.unlink(missing_ok=True)
            return [TextContent(type="text", text=json.dumps({"status": "success", "message": f"Already logged in as {account}. Cookies saved.", "account": account}))]
        await browser.close()
        await pw.stop()
    except Exception as e:
        logger.warning(f"Could not pre-load cookies via CDP: {e}")

    return [TextContent(type="text", text=json.dumps({
        "status": "waiting",
        "message": f"Browser open (PID {proc.pid}). Log in manually, then call login_linkedin_save.",
        "account": account, "cdp_port": port, "next_step": "login_linkedin_save"
    }))]


async def do_login_save(account: str = "carlos"):
    """Step 2: Reconnect to the open browser via CDP, save cookies, close it."""
    port_file = _login_port_file(account)

    if not port_file.exists():
        return [TextContent(type="text", text=json.dumps({
            "status": "error",
            "message": f"No open login session found for {account}. Call login_linkedin first."
        }))]

    _port_data = port_file.read_text().strip().split(":")
    port = int(_port_data[0])
    _proc_pid = int(_port_data[1]) if len(_port_data) > 1 else None

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx:
            await browser.close()
            await pw.stop()
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Browser context not found. Try logging in again."}))]

        pages = ctx.pages
        page = pages[0] if pages else None
        if not page:
            await browser.close()
            await pw.stop()
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "No page found in browser."}))]

        current_url = page.url
        logger.info(f"login_save: current URL = {current_url}")

        if "linkedin.com/login" in current_url or "checkpoint" in current_url:
            await browser.close()
            await pw.stop()
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "message": "Still on login/checkpoint page. Finish logging in first, then call login_linkedin_save again."
            }))]

        await save_cookies(page, account, cdp_port=port)
        await browser.close()
        await pw.stop()
        port_file.unlink(missing_ok=True)
        # Kill the whole Chromium tree by matching the CDP port
        import subprocess as _kill_sp
        try:
            _kill_sp.run(
                ["pkill", "-f", f"remote-debugging-port={port}"],
                capture_output=True
            )
            logger.info(f"Killed Chromium tree for CDP port {port}")
        except Exception as _ke:
            logger.warning(f"Could not kill Chromium: {_ke}")

    except Exception as e:
        port_file.unlink(missing_ok=True)
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Could not connect to browser: {e}. Try login_linkedin again."}))]

    return [TextContent(type="text", text=json.dumps({
        "status": "success",
        "message": f"Login session saved for {account}. Browser closed.",
        "account": account
    }))]


async def do_browse_feed(count: int, max_age_days: int = 0):
    """Browse LinkedIn feed - Using discovered selectors Dec 2025
    
    Args:
        count: Number of posts to retrieve
        max_age_days: Filter posts older than this (0 = no filter)
    """
    posts = []

    if err := require_session(): return err

    async with BrowserSession(headless=True) as session:
        page = await session.new_page('https://www.linkedin.com/feed/')

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]

        # Wait for feed to fully load
        await page.wait_for_timeout(5000)

        # Scroll and collect posts
        for scroll_attempt in range(min(count + 2, 10)):
            new_posts = await page.evaluate('''() => {
                const posts = [];

                // Find posts by data-urn attribute containing activity (Dec 2025 selectors)
                const containers = document.querySelectorAll('div.feed-shared-update-v2[data-urn^="urn:li:activity"]');

                containers.forEach(container => {
                    try {
                        const urn = container.getAttribute('data-urn');
                        if (!urn) return;

                        // Author - use update-components-actor__title (Dec 2025)
                        const authorEl = container.querySelector('.update-components-actor__title span');
                        const author = authorEl ? authorEl.innerText.trim().split('\\n')[0] : 'Unknown';

                        // Post date/time - in sub-description
                        const timeEl = container.querySelector('.update-components-actor__sub-description');
                        const date = timeEl ? timeEl.innerText.trim().split('\\n')[0] : '';

                        // Post content - use feed-shared-inline-show-more-text
                        const contentEl = container.querySelector(
                            '.feed-shared-update-v2__description .feed-shared-inline-show-more-text span[dir="ltr"], ' +
                            '.feed-shared-inline-show-more-text span[dir="ltr"], ' +
                            '.feed-shared-update-v2__description span[dir="ltr"]'
                        );
                        const content = contentEl ? contentEl.innerText.trim().substring(0, 1000) : '';

                        // Reactions - look for social counts
                        const reactionsEl = container.querySelector('.social-details-social-counts__reactions-count, [class*="reactions-count"]');
                        const reactions = reactionsEl ? reactionsEl.innerText.trim() : '0';

                        // Comments count
                        const commentsEl = container.querySelector('.social-details-social-counts__comments, button[aria-label*="comment"] span');
                        const comments = commentsEl ? commentsEl.innerText.trim() : '0';

                        const postUrl = 'https://www.linkedin.com/feed/update/' + urn + '/';

                        if (content || author !== 'Unknown') {
                            posts.push({ urn, url: postUrl, author, date, content, reactions, comments });
                        }
                    } catch (e) { console.error(e); }
                });

                return posts;
            }''')

            for p in new_posts:
                if not any(existing['urn'] == p['urn'] for existing in posts):
                    posts.append(p)

            if len(posts) >= count:
                break

            await page.evaluate('window.scrollBy(0, 600)')
            await page.wait_for_timeout(1500)

        await save_cookies(page)
        
        # Apply date filter if specified
        if max_age_days > 0:
            filtered_posts = filter_posts_by_age(posts, max_age_days)
        else:
            # Still add parsed dates for reference
            for post in posts:
                parsed_date, age_days = parse_linkedin_date(post.get('date', ''))
                post['parsed_date'] = parsed_date.strftime('%Y-%m-%d')
                post['age_days'] = age_days
            filtered_posts = posts
        
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "posts": filtered_posts[:count],
            "total_found": len(posts),
            "total_after_filter": len(filtered_posts),
            "max_age_days": max_age_days
        }, indent=2))]


async def do_search_profiles(query: str, count: int):
    """Search for LinkedIn profiles - Updated selectors"""
    import urllib.parse
    encoded_query = urllib.parse.quote(query)

    if err := require_session(): return err

    async with BrowserSession(headless=True) as session:
        page = await session.new_page(f'https://www.linkedin.com/search/results/people/?keywords={encoded_query}')

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]

        # Wait for search results to load
        await page.wait_for_timeout(5000)

        # Extract profiles - LinkedIn now uses obfuscated classes, so we find by profile links
        profiles = await page.evaluate('''(count) => {
            const profiles = [];
            const seenUrls = new Set();
            
            // Find all links to profiles
            const profileLinks = document.querySelectorAll('a[href*="/in/"]');
            
            profileLinks.forEach(link => {
                if (profiles.length >= count) return;
                
                const url = link.href.split('?')[0];
                if (seenUrls.has(url)) return;
                
                // Get the text content which usually contains name and info
                const text = link.innerText.trim();
                if (!text || text.length < 3) return;
                
                // Look for the parent container that has all the info
                // Go up to find a container with more details
                let container = link.closest('li') || link.parentElement?.parentElement?.parentElement;
                
                // Extract name - usually the first line or the link text
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                let name = lines[0] || 'Unknown';
                // Remove connection indicators
                name = name.replace(/\\s*•\\s*\\d+(st|nd|rd|th)$/, '').trim();
                
                // Try to find headline and location from the container
                let headline = '';
                let location = '';
                let connectionDegree = '';
                
                if (container) {
                    const allText = container.innerText || '';
                    const allLines = allText.split('\\n').map(l => l.trim()).filter(l => l && l.length > 2);
                    
                    // Parse the lines - typically: Name, degree, headline, location, mutual connections
                    for (let i = 0; i < allLines.length; i++) {
                        const line = allLines[i];
                        if (line.match(/^\\d+(st|nd|rd|th)$/)) {
                            connectionDegree = line;
                        } else if (line.includes('•') && line.match(/\\d+(st|nd|rd|th)/)) {
                            connectionDegree = line.match(/\\d+(st|nd|rd|th)/)?.[0] || '';
                        } else if (!headline && i > 0 && !line.match(/^(Connect|Message|Follow)/) && line.length > 10) {
                            headline = line;
                        } else if (headline && !location && !line.match(/^(Connect|Message|Follow|\\d+ mutual)/) && line.length > 3) {
                            location = line;
                        }
                    }
                }
                
                if (name !== 'Unknown' && name.length > 1) {
                    seenUrls.add(url);
                    profiles.push({
                        name: name,
                        headline: headline.substring(0, 200),
                        location: location.substring(0, 100),
                        url: url,
                        connection_degree: connectionDegree
                    });
                }
            });
            
            return profiles;
        }''', count)

        await save_cookies(page)
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "profiles": profiles,
            "count": len(profiles),
            "query": query
        }, indent=2))]


async def do_search_posts(
    query: str, count: int, max_age_days: int = 0,
    date_posted: str | None = None, sort_by: str | None = None,
    author_name: str | None = None, account: str = "carlos",
):
    """Search LinkedIn posts via DOM text parsing with native URL filters."""
    import urllib.parse as _up

    if err := require_session(account): return err

    # Build search URL with LinkedIn native filters
    params = {"keywords": query, "origin": "FACETED_SEARCH"}
    if date_posted:
        params["datePosted"] = f'"{date_posted}"'
    if sort_by:
        params["sortBy"] = f'"{sort_by}"'
    url = "https://www.linkedin.com/search/results/content/?" + _up.urlencode(params, quote_via=_up.quote)
    logger.info(f"search_posts_v2 URL: {url}")

    async with BrowserSession(headless=True, account=account) as session:
        page = await session.new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]

        await page.wait_for_timeout(5000)

        # Scroll to load more posts if needed
        max_scrolls = max(0, (count - 5) // 5) + 1
        if count <= 6:
            max_scrolls = 0
        for scroll_i in range(max_scrolls):
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            await page.wait_for_timeout(3000)
            # Click "Load more" button if present
            await page.evaluate(r'''() => {
                const btns = [...document.querySelectorAll('button')];
                const lb = btns.find(b => b.innerText.trim().toLowerCase() === 'load more');
                if (lb) lb.click();
            }''')
            await page.wait_for_timeout(1500)
            logger.info(f"Scroll {scroll_i+1}/{max_scrolls}")

        # Extract main text
        main_text = await page.evaluate(r'''() => {
            const main = document.querySelector('main');
            return main ? main.innerText : '';
        }''')

        await save_cookies(page)

    if not main_text:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "No content on search results page"}))]

    # Parse posts from DOM text
    posts = _parse_search_posts(main_text)
    logger.info(f"search_posts_v2 parsed {len(posts)} posts")

    # Client-side author filter
    if author_name:
        author_lower = author_name.lower()
        posts = [p for p in posts if author_lower in p["author"].lower()]

    # Client-side age filter (legacy)
    if max_age_days > 0:
        posts = [p for p in posts if p["age_days"] <= max_age_days]

    return [TextContent(type="text", text=json.dumps({
        "status": "success",
        "posts": posts[:count],
        "total_found": len(posts),
        "query": query,
        "filters": {
            "date_posted": date_posted,
            "sort_by": sort_by,
            "author_name": author_name,
            "max_age_days": max_age_days if max_age_days > 0 else None,
        }
    }, indent=2))]


def _parse_search_posts(main_text: str) -> list[dict]:
    """Parse LinkedIn search results from main.innerText into structured post dicts."""
    raw_blocks = re.split(r'(?:^|\n)Feed post\n', main_text)
    raw_blocks = [b for b in raw_blocks[1:] if b.strip()]

    posts = []
    for block in raw_blocks:
        lines = block.strip().split('\n')

        # Author: first non-empty line (skip "Feed post" artifacts)
        author = ''
        for line in lines:
            line = line.strip()
            if line and line != 'Feed post':
                author = line
                break
        # Clean author name
        author = re.sub(r'\s*(Verified|Premium)\s*Profile\s*', '', author).strip()
        author = re.sub(r'\s*\d+(st|nd|rd|th)\+?\s*$', '', author).strip()
        author = re.sub(r'\s*,\s*Open to work\s*', '', author).strip()

        # Date: relative time pattern in header (before "Follow")
        date_raw = ''
        follow_pos = block.find('\nFollow\n')
        header = block[:follow_pos] if follow_pos > 0 else block[:500]
        date_m = re.search(r'\n(\d+(?:m|min|h|d|w|mo|yr))\s*(?:•|$)', header, re.MULTILINE)
        if date_m:
            date_raw = date_m.group(1)
        elif re.search(r'just now', header, re.IGNORECASE):
            date_raw = '0m'

        # Content: between "Follow\n" (or date line) and social action buttons
        content = ''
        content_start = -1
        if follow_pos >= 0:
            content_start = follow_pos + len('\nFollow\n')
        elif date_m:
            # No "Follow" button (e.g., company page posts you own).
            # Content starts after the date line "Xm • \n"
            date_line_end = block.find('\n', date_m.end())
            if date_line_end >= 0:
                content_start = date_line_end + 1

        if content_start >= 0:
            after = block[content_start:]
            end_pos = len(after)
            for pat in [r'\n\d+ reactions?\n', r'\n\d+ comments?\n', r'\nLike\nComment\nRepost\nSend']:
                m = re.search(pat, after)
                if m and m.start() < end_pos:
                    end_pos = m.start()
            content = after[:end_pos].strip()
            content = re.sub(r'\nhashtag\n', '\n', content)
            content = re.sub(r'\n… more\s*$', '', content).strip()

        # Engagement counts
        reactions = 0
        r_m = re.search(r'(\d+)\s+reactions?', block)
        if r_m: reactions = int(r_m.group(1))
        comments = 0
        c_m = re.search(r'(\d+)\s+comments?', block)
        if c_m: comments = int(c_m.group(1))

        date_info = parse_linkedin_date(date_raw)

        if not content and not author:
            continue

        posts.append({
            "author": author,
            "date": date_raw,
            "age_days": date_info[1],
            "parsed_date": date_info[0].strftime('%Y-%m-%d'),
            "content": content[:2000],
            "reactions": reactions,
            "comments": comments,
            "url": None,
        })

    return posts

async def do_view_profile(profile_url: str):
    """View a LinkedIn profile - Updated selectors"""
    if 'linkedin.com/in/' not in profile_url:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Invalid LinkedIn profile URL"}))]

    if err := require_session(): return err

    async with BrowserSession(headless=True) as session:
        page = await session.new_page(profile_url)

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]

        # Wait for profile card to load
        try:
            await page.wait_for_selector('.pv-top-card, .scaffold-layout__main', timeout=15000)
        except:
            logger.warning("Profile card selector timeout")

        await page.wait_for_timeout(3000)

        # Extract profile data - using robust approach since LinkedIn uses obfuscated classes
        profile = await page.evaluate('''() => {
            const data = {};

            // Name - find h1 with inline class (most reliable)
            const nameEl = document.querySelector('h1.inline, h1[class*="inline"]');
            data.name = nameEl ? nameEl.innerText.trim() : null;

            // Headline - text-body-medium near the name, usually the first one in top card
            const topCard = document.querySelector('.pv-top-card, [class*="top-card"]') || document;
            // Try multiple approaches for headline
            let headline = null;
            // First try: .text-body-medium directly
            const headlineEl = document.querySelector('.text-body-medium');
            if (headlineEl) {
                headline = headlineEl.innerText.trim();
            }
            // Second try: look for div right after h1
            if (!headline) {
                const h1 = document.querySelector('h1.inline, h1[class*="inline"]');
                if (h1 && h1.nextElementSibling) {
                    headline = h1.nextElementSibling.innerText.trim();
                }
            }
            data.headline = headline;

            // Location - look for location pattern in top card area
            // Usually appears after headline, contains city/country
            const textSmalls = topCard.querySelectorAll('.text-body-small span');
            for (const el of textSmalls) {
                const text = el.innerText.trim();
                // Skip if it contains connection/follower info or is too long
                if (text && text.length > 3 && text.length < 80 &&
                    !text.includes('connection') && !text.includes('follower') &&
                    !text.includes('Contact') && !text.includes('degree')) {
                    data.location = text;
                    break;
                }
            }

            // Connection info - look for text with "connection" or "follower"
            const allText = document.body.innerText;
            const connMatch = allText.match(/(\\d+[\\+,]?\\d*\\s*(connections?|followers?))/i);
            data.connections = connMatch ? connMatch[0] : null;

            // About section - find section with id="about" and get the actual content
            const aboutSection = document.querySelector('#about');
            if (aboutSection) {
                const aboutContainer = aboutSection.closest('section');
                if (aboutContainer) {
                    // Find the span with actual about text (not the heading)
                    const spans = aboutContainer.querySelectorAll('span[aria-hidden="true"]');
                    for (const span of spans) {
                        const text = span.innerText.trim();
                        if (text && text.length > 20 && text !== 'About') {
                            data.about = text.substring(0, 500);
                            break;
                        }
                    }
                }
            }
            
            // Experience - get current role from experience section
            const expSection = document.querySelector('#experience');
            if (expSection) {
                const expContainer = expSection.closest('section');
                if (expContainer) {
                    // First role title
                    const roleEl = expContainer.querySelector('div[data-view-name="profile-component-entity"] span[aria-hidden="true"]');
                    if (roleEl) {
                        data.current_role = roleEl.innerText.trim();
                    }
                    
                    // Company name - usually in a link
                    const companyLink = expContainer.querySelector('a[href*="/company/"] span[aria-hidden="true"]');
                    if (companyLink) {
                        data.current_company = companyLink.innerText.trim();
                    }
                }
            }

            return data;
        }''')

        await save_cookies(page)
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "profile": profile,
            "url": profile_url
        }, indent=2))]


async def do_get_posts(profile_url: str, count: int = 5):
    """Get posts from a LinkedIn profile or company page, returning URLs and content."""
    if err := require_session(): return err

    # Normalise URL — accept short slugs like "ecosemantic" or "/company/ecosemantic"
    url = profile_url.strip().rstrip("/")
    if not url.startswith("http"):
        if "/" not in url:
            # bare slug — guess type: company if no personal indicator
            url = f"https://www.linkedin.com/company/{url}/posts/"
        else:
            url = f"https://www.linkedin.com/{url.lstrip('/')}"
    if not url.endswith("/posts/") and not url.endswith("/posts"):
        url = url.rstrip("/") + "/posts/"

    async with BrowserSession(headless=True) as session:
        page = await session.new_page(url)

        if "login" in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]

        await page.wait_for_timeout(4000)

        posts = await page.evaluate('''(count) => {
            const results = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/feed/update/"]');
            links.forEach(a => {
                if (results.length >= count) return;
                // Grab the clean URL (strip query params)
                const href = a.href.split("?")[0];
                if (seen.has(href)) return;
                seen.add(href);

                // Walk up to find the post container
                let container = a.closest(".feed-shared-update-v2") ||
                                a.closest("[data-urn]") ||
                                a.closest("li") ||
                                a.parentElement;

                const content = container ? container.innerText.trim().substring(0, 300) : "";
                // Extract URN from href
                const urnMatch = href.match(/urn:li:activity:\d+/);
                results.push({
                    url: href,
                    urn: urnMatch ? urnMatch[0] : null,
                    preview: content.replace(/\s+/g, " ").substring(0, 200)
                });
            });
            return results;
        }''', count)

        await save_cookies(page)
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "posts": posts,
            "count": len(posts),
            "source": url
        }, indent=2))]


async def do_interact_post(post_url: str, action: str, comment: str = None, company_id: str = None, account: str = None):
    """Interact with a LinkedIn post - read, like, or comment.
    
    Args:
        post_url: LinkedIn post URL
        action: 'read', 'like', or 'comment'
        comment: Comment text (required for action='comment')
        company_id: Optional company/showcase ID to like/comment as (loaded from LINKEDIN_CARLOS_COMPANY_ID env for carlos)
        account: Which account to use ('carlos' or 'claudia')
    """
    if 'linkedin.com/posts/' not in post_url and 'linkedin.com/feed/update/' not in post_url:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Invalid LinkedIn post URL"}))]

    if err := require_session(account): return err

    # Add company_id to URL if provided (to comment as company page)
    if company_id and action == "comment":
        separator = '&' if '?' in post_url else '?'
        post_url = f"{post_url}{separator}actorCompanyId={company_id}"
        logger.info(f"Commenting as company ID: {company_id}")

    async with BrowserSession(headless=True, account=account) as session:
        page = await session.new_page(post_url)

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Not logged in for account: {account}"}))]

        # Wait for post to load
        await page.wait_for_timeout(5000)
        
        # Handle COMMENT action
        if action == "comment":
            if not comment:
                return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Comment text required"}))]
            
            try:
                # Click comment button to open comment box
                comment_btn = page.locator('button[aria-label*="Comment"], button[aria-label*="comment"]')
                if await comment_btn.count() > 0:
                    await comment_btn.first.click()
                    await page.wait_for_timeout(1500)
                
                # Find the comment input box - it's a contenteditable div
                comment_box = page.locator('.ql-editor[data-placeholder*="comment"], .comments-comment-box__form .ql-editor, div[contenteditable="true"][aria-placeholder*="comment"]')
                
                if await comment_box.count() == 0:
                    # Try clicking on placeholder text
                    placeholder = page.locator('div[data-placeholder*="Add a comment"]')
                    if await placeholder.count() > 0:
                        await placeholder.first.click()
                        await page.wait_for_timeout(1000)
                        comment_box = page.locator('.ql-editor[contenteditable="true"]')
                
                if await comment_box.count() == 0:
                    await save_cookies(page)
                    return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find comment input box"}))]
                
                # Type the comment
                await comment_box.first.click()
                await page.wait_for_timeout(500)
                await comment_box.first.fill(comment)
                await page.wait_for_timeout(1000)
                
                logger.info(f"Comment text entered: {comment[:50]}...")
                
                # Click the Comment submit button (the blue button that appears after typing)
                # NOT the "Post" button which is for Repost!
                submit_selectors = [
                    'button.comments-comment-box__submit-button--cr',  # Current LinkedIn class (Dec 2025)
                    'button.comments-comment-box__submit-button',
                    'form.comments-comment-box__form button.artdeco-button--primary',
                    'div.comments-comment-box button.artdeco-button--primary',
                    'button[type="submit"].artdeco-button--primary',
                ]
                
                submit_btn = None
                for selector in submit_selectors:
                    btn = page.locator(selector)
                    if await btn.count() > 0:
                        submit_btn = btn
                        logger.info(f"Found submit button with selector: {selector}")
                        break
                
                if submit_btn is None:
                    # Find button with text "Comment" (not "Post" which is repost!)
                    comment_buttons = page.locator('button:has-text("Comment"):visible')
                    count = await comment_buttons.count()
                    logger.info(f"Found {count} buttons with 'Comment' text")
                    
                    # Look for the primary/blue button specifically
                    for i in range(count):
                        btn = comment_buttons.nth(i)
                        classes = await btn.get_attribute('class') or ''
                        if 'artdeco-button--primary' in classes:
                            submit_btn = btn
                            logger.info(f"Found primary Comment button at index {i}")
                            break
                
                if submit_btn and await submit_btn.count() > 0:
                    is_enabled = await submit_btn.first.is_enabled()
                    logger.info(f"Submit button enabled: {is_enabled}")
                    
                    if is_enabled:
                        await submit_btn.first.click()
                        logger.info("Clicked submit button")
                        await page.wait_for_timeout(3000)
                        
                        # Verify comment was posted by checking if comment box is now empty
                        try:
                            new_content = await comment_box.first.inner_text()
                            if len(new_content.strip()) < 5:
                                logger.info("Comment box cleared - likely posted successfully")
                                await save_cookies(page)
                                return [TextContent(type="text", text=json.dumps({"status": "success", "action": "comment", "message": "Comment posted successfully"}))]
                        except:
                            pass
                        
                        await save_cookies(page)
                        return [TextContent(type="text", text=json.dumps({"status": "uncertain", "action": "comment", "message": "Submit clicked but could not verify if posted"}))]
                    else:
                        await save_cookies(page)
                        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Submit button found but not enabled"}))]
                else:
                    await save_cookies(page)
                    return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find submit button"}))]
                    
            except Exception as e:
                logger.error(f"Comment error: {e}")
                await save_cookies(page)
                return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Failed to post comment: {str(e)}"}))]
        
        # Handle LIKE action
        if action == "like":
            try:
                # Step 1: Switch identity if company_id provided (like as company page)
                if company_id:
                    identity_btn = page.locator('button.content-admin-identity-toggle-button, button[aria-label*="switching identity"]')
                    if await identity_btn.count() > 0:
                        await identity_btn.first.click()
                        await page.wait_for_timeout(1500)
                        # Modal uses name="actorSelector" radios with ids like select-self, select-ecosemantic, select-kimel-tech
                        # Try to find the right radio by company_id or by label text
                        try:
                            company_radio = await page.evaluate(f"""(companyId) => {{
                                const radios = document.querySelectorAll('input[name="actorSelector"]');
                                for (const r of radios) {{
                                    if (r.id === 'select-self') continue;
                                    const li = r.closest('li');
                                    if (!li) continue;
                                    const text = li.innerText.toLowerCase();
                                    if (text.includes('ecosemantic')) return r.id;
                                }}
                                for (const r of radios) {{
                                    if (r.id !== 'select-self') return r.id;
                                }}
                                return null;
                            }}""", company_id)
                        except Exception as eval_err:
                            logger.error(f"Company radio evaluate failed: {eval_err}")
                            company_radio = None
                        if company_radio:
                            # Use JS click — the radio input may be hidden/styled and Playwright's click hangs
                            await page.evaluate(f"document.getElementById('{company_radio}').click()")
                            await page.wait_for_timeout(800)
                            # Wait for Save button to become enabled after radio selection
                            save_btn = page.locator('button[aria-label="Save selection"]:not([disabled]), button:has-text("Save"):not([disabled])')
                            try:
                                await save_btn.first.wait_for(state='visible', timeout=3000)
                                await save_btn.first.click()
                                await page.wait_for_timeout(1500)
                            except Exception as save_err:
                                logger.warning(f"Save button not clickable: {save_err}")
                            logger.info(f"Switched identity to {company_radio} (company_id: {company_id})")
                        else:
                            logger.warning("Could not find company radio in identity modal")
                    else:
                        logger.warning("Could not find identity switcher button")

                # Step 2: Click Like
                like_btn = page.locator('button[aria-label*="Like"], button[aria-label*="React Like"]')
                if await like_btn.count() > 0:
                    await like_btn.first.click()
                    await page.wait_for_timeout(2000)
                    await save_cookies(page)
                    actor = f"company:{company_id}" if company_id else f"personal:{account}"
                    return [TextContent(type="text", text=json.dumps({"status": "success", "action": "like", "message": f"Post liked as {actor}"}))]
                else:
                    await save_cookies(page)
                    return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find like button"}))]
            except Exception as e:
                await save_cookies(page)
                return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Failed to like: {str(e)}"}))]

        # Handle READ action (default) - Click to load comments if there's a comments button
        try:
            comments_btn = page.locator('button[aria-label*="comment"], .social-details-social-counts__comments')
            if await comments_btn.count() > 0:
                await comments_btn.first.click()
                await page.wait_for_timeout(2000)
        except:
            pass

        # Extract post content and comments
        post_data = await page.evaluate('''() => {
            const data = { post: {}, comments: [] };

            // Author - use updated selectors
            const authorEl = document.querySelector('.update-components-actor__title span');
            data.post.author = authorEl ? authorEl.innerText.trim().split('\\n')[0] : 'Unknown';

            // Author headline/description
            const authorDescEl = document.querySelector('.update-components-actor__description');
            data.post.author_headline = authorDescEl ? authorDescEl.innerText.trim() : '';

            // Post date
            const dateEl = document.querySelector('.update-components-actor__sub-description');
            data.post.date = dateEl ? dateEl.innerText.trim().split('\\n')[0] : '';

            // Content
            const contentEl = document.querySelector(
                '.feed-shared-update-v2__description .feed-shared-inline-show-more-text span[dir="ltr"], ' +
                '.feed-shared-inline-show-more-text span[dir="ltr"]'
            );
            data.post.content = contentEl ? contentEl.innerText.trim() : '';

            // Reactions
            const reactionsEl = document.querySelector('.social-details-social-counts__reactions-count');
            data.post.reactions = reactionsEl ? reactionsEl.innerText.trim() : '0';

            // Comments count
            const commentsCountEl = document.querySelector('.social-details-social-counts__comments');
            data.post.comments_count = commentsCountEl ? commentsCountEl.innerText.trim() : '0';

            // Extract comments using correct selectors (Dec 2025)
            // Use comments-comment-entity as the main container for each comment
            const commentElements = document.querySelectorAll('.comments-comment-entity');
            const seenComments = new Set();
            
            commentElements.forEach(commentEl => {
                try {
                    // Comment author - in description-title
                    const commentAuthorEl = commentEl.querySelector('.comments-comment-meta__description-title');
                    const commentAuthor = commentAuthorEl ? commentAuthorEl.innerText.trim().split('\\n')[0] : 'Unknown';
                    
                    // Author headline - in description-subtitle
                    const authorHeadlineEl = commentEl.querySelector('.comments-comment-meta__description-subtitle');
                    const authorHeadline = authorHeadlineEl ? authorHeadlineEl.innerText.trim().split('\\n')[0] : '';
                    
                    // Comment text - in main-content
                    const commentTextEl = commentEl.querySelector('.comments-comment-item__main-content');
                    const commentText = commentTextEl ? commentTextEl.innerText.trim() : '';
                    
                    // Comment date - look for time element or text with time pattern
                    const timeEl = commentEl.querySelector('time, .comments-comment-item__timestamp');
                    let commentDate = '';
                    if (timeEl) {
                        commentDate = timeEl.innerText.trim();
                    } else {
                        // Try to find date in the meta area (usually like "5d" or "2h")
                        const metaText = commentEl.querySelector('.comments-comment-meta__data');
                        if (metaText) {
                            const match = metaText.innerText.match(/(\\d+[hdwmo]|\\d+ (?:hour|day|week|month|year)s? ago)/i);
                            if (match) commentDate = match[0];
                        }
                    }
                    
                    // Create unique key to avoid duplicates
                    const uniqueKey = commentAuthor + '|' + commentText.substring(0, 50);
                    
                    if (commentText && commentAuthor !== 'Unknown' && !seenComments.has(uniqueKey)) {
                        seenComments.add(uniqueKey);
                        data.comments.push({
                            author: commentAuthor,
                            author_headline: authorHeadline.substring(0, 150),
                            text: commentText.substring(0, 500),
                            date: commentDate
                        });
                    }
                } catch (e) { console.error(e); }
            });

            return data;
        }''')

        await save_cookies(page)
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "action": action,
            "post": post_data["post"],
            "comments": post_data["comments"],
            "comments_found": len(post_data["comments"])
        }, indent=2))]


async def do_create_post(content: str, company_id: str = None, account: str = None, group_name: str = None):
    """Create a new LinkedIn post, optionally as a company page or to a group.

    Proven flow (tested step-by-step):
      1. Navigate to /feed/, click "Start a post"
      2a. If group_name: settings > Group radio > match group > select > Save > Done
      2b. Elif company_id: settings button > actor toggle > select radio > Save > Done
      3. Type content into ql-editor via keyboard.type()
      4. Click Post button (share-actions__primary-action)
    """
    if not content or not content.strip():
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Post content cannot be empty"}))]

    if err := require_session(account):
        return err

    async with BrowserSession(headless=True, account=account) as session:
        page = await session.new_page("https://www.linkedin.com/feed/")

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Not logged in for account: {account}"}))]

        await page.wait_for_timeout(3000)

        # --- Step 1: Open the post editor modal ---
        start_btn = page.locator('button:has-text("Start a post")')
        if await start_btn.count() == 0:
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find 'Start a post' button"}))]

        await start_btn.first.click()
        await page.wait_for_timeout(2000)
        logger.info("Opened post editor modal")

        # --- Step 2a: Select group if group_name provided ---
        if group_name:
            try:
                # Open Post settings
                await page.locator('button.share-unified-settings-entry-button').first.click()
                await page.wait_for_timeout(2000)

                # Click the Group visibility radio
                await page.evaluate("document.getElementById('sharing-shared-generic-list-radio-CONTAINER').click()")
                await page.wait_for_timeout(2000)

                # Find matching group radio by name (case-insensitive partial match)
                matched = await page.evaluate(f"""(target) => {{
                    const radios = document.querySelectorAll('input[type="radio"][id*="fsd_group"]');
                    for (const r of radios) {{
                        const li = r.closest('li') || r.parentElement;
                        if (li && li.innerText.toLowerCase().includes(target.toLowerCase())) {{
                            return {{id: r.id, name: li.innerText.trim().split('\\n')[0].trim()}};
                        }}
                    }}
                    return null;
                }}""", group_name)

                if not matched:
                    await save_cookies(page)
                    return [TextContent(type="text", text=json.dumps({
                        "status": "error",
                        "message": f"No group matching '{group_name}' found. Use list_linkedin_groups to see available groups."
                    }))]

                # Select the matched group
                await page.evaluate(f"document.getElementById('{matched['id']}').click()")
                await page.wait_for_timeout(1000)

                # Click Save
                save_btn = page.locator('button.share-box-footer__primary-btn:has-text("Save")')
                await save_btn.first.click()
                await page.wait_for_timeout(2000)

                # Click Done to return to editor
                await page.locator('button:has-text("Done")').first.click()
                await page.wait_for_timeout(2000)
                logger.info(f"Selected group: {matched['name']} ({matched['id']})")

            except Exception as e:
                logger.warning(f"Group selection failed: {e} — aborting")
                await save_cookies(page)
                return [TextContent(type="text", text=json.dumps({
                    "status": "error",
                    "message": f"Group selection failed: {e}"
                }))]

        # --- Step 2b: Switch identity if company page requested (skip if group) ---
        elif company_id:
            try:
                # 2a. Open Post settings
                await page.locator('button.share-unified-settings-entry-button').first.click()
                await page.wait_for_timeout(1500)

                # 2b. Click author name to open "Posting as"
                await page.locator('button.share-unified-settings-menu__actor-toggle').first.click()
                await page.wait_for_timeout(1500)

                # 2c. Find and click the target radio by label text
                target_label = "ecosemantic"  # default
                radio_id = await page.evaluate(f"""(target) => {{
                    const radios = document.querySelectorAll('input[type="radio"]');
                    for (const r of radios) {{
                        const li = r.closest('li') || r.parentElement;
                        if (li && li.innerText.toLowerCase().includes(target)) return r.id;
                    }}
                    return null;
                }}""", target_label)

                if radio_id:
                    await page.evaluate(f"document.getElementById('{radio_id}').click()")
                    await page.wait_for_timeout(800)

                    # 2d. Click Save (becomes enabled after radio change)
                    save_btn = page.locator('button.share-box-footer__primary-btn:has-text("Save")')
                    await save_btn.first.click()
                    await page.wait_for_timeout(2000)

                    # 2e. Click Done to return to editor
                    await page.locator('button:has-text("Done")').first.click()
                    await page.wait_for_timeout(2000)
                    logger.info(f"Switched identity to {target_label} ({radio_id})")
                else:
                    logger.warning(f"Could not find radio for '{target_label}' — posting as default identity")
                    # Go back to editor
                    await page.locator('button:has-text("Back")').first.click()
                    await page.wait_for_timeout(500)
                    await page.locator('button:has-text("Done")').first.click()
                    await page.wait_for_timeout(1000)
            except Exception as e:
                logger.warning(f"Identity switching failed: {e} — attempting to continue")

        # --- Step 3: Type the post content ---
        editor = page.locator('div.ql-editor[role="textbox"]')
        if await editor.count() == 0:
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find post editor"}))]

        await editor.first.click()
        await page.wait_for_timeout(300)
        await page.keyboard.type(content, delay=20)
        await page.wait_for_timeout(1000)
        logger.info(f"Typed post content ({len(content)} chars)")

        # --- Step 4: Click Post ---
        post_btn = page.locator('button.share-actions__primary-action')
        if await post_btn.first.is_disabled():
            await page.wait_for_timeout(2000)

        if await post_btn.first.is_disabled():
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Post button still disabled after typing"}))]

        await post_btn.first.click()
        logger.info("Clicked Post button")
        await page.wait_for_timeout(5000)

        # Verify: the share-creation editor should disappear after successful post
        dialog_count = await page.locator('div.share-creation-state, div.ql-editor').count()
        actor = f"group:{group_name}" if group_name else (f"company:{company_id}" if company_id else f"personal:{account}")
        await save_cookies(page)

        if dialog_count == 0:
            return [TextContent(type="text", text=json.dumps({
                "status": "success",
                "action": "create_post",
                "message": f"Post published as {actor}",
                "content_preview": content[:100]
            }))]
        else:
            return [TextContent(type="text", text=json.dumps({
                "status": "uncertain",
                "action": "create_post",
                "message": f"Post button clicked as {actor} but dialog may still be open"
            }))]


async def do_list_groups(account: str = None):
    """List all LinkedIn groups the account is a member of.

    Flow: Open editor → Post settings → Group radio → scrape group list → close.
    """
    if err := require_session(account):
        return err

    async with BrowserSession(headless=True, account=account) as session:
        page = await session.new_page("https://www.linkedin.com/feed/")

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Not logged in for account: {account}"}))]

        await page.wait_for_timeout(3000)

        # Open editor
        start_btn = page.locator('button:has-text("Start a post")')
        if await start_btn.count() == 0:
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find 'Start a post' button"}))]

        await start_btn.first.click()
        await page.wait_for_timeout(2000)

        # Open Post settings
        await page.locator('button.share-unified-settings-entry-button').first.click()
        await page.wait_for_timeout(2000)

        # Click Group radio to show group list
        await page.evaluate("document.getElementById('sharing-shared-generic-list-radio-CONTAINER').click()")
        await page.wait_for_timeout(2000)

        # Scrape all group radios
        groups = await page.evaluate('''() => {
            const radios = document.querySelectorAll('input[type="radio"][id*="fsd_group"]');
            return Array.from(radios).map(r => {
                const li = r.closest('li') || r.parentElement;
                const text = li ? li.innerText.trim() : "";
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                const name = lines[0] || "";
                const visibility = lines.find(l => l === "Public" || l === "Private") || "Unknown";
                // Extract URN from radio id: sharing-shared-generic-list-radio-urn:li:fsd_group:NNNNN
                const urn = r.id.replace('sharing-shared-generic-list-radio-', '');
                return {name, urn, visibility};
            });
        }''')

        await save_cookies(page)
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "account": account,
            "groups_count": len(groups),
            "groups": groups
        }, indent=2))]


async def do_delete_post(post_url: str, account: str = None):
    """Delete a LinkedIn post by navigating to its URL.

    Tested flow:
      1. Navigate to post URL
      2. Click ••• menu (feed-shared-control-menu__trigger)
      3. Click "Delete post" from dropdown
      4. Click "Delete" in confirmation dialog
    """
    if 'linkedin.com' not in post_url:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Invalid LinkedIn URL"}))]

    if err := require_session(account):
        return err

    async with BrowserSession(headless=True, account=account) as session:
        page = await session.new_page(post_url)

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Not logged in for account: {account}"}))]

        await page.wait_for_timeout(5000)

        # Step 1: Click ••• menu
        menu_btn = page.locator('button.feed-shared-control-menu__trigger')
        if await menu_btn.count() == 0:
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Could not find post control menu (•••). You may not own this post."}))]

        await menu_btn.first.click()
        await page.wait_for_timeout(1500)

        # Step 2: Click "Delete post"
        delete_item = page.locator('.artdeco-dropdown__content--is-open >> text="Delete post"')
        if await delete_item.count() == 0:
            await page.keyboard.press("Escape")
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "No 'Delete post' option in menu. You may not own this post."}))]

        await delete_item.first.click()
        await page.wait_for_timeout(2000)

        # Step 3: Confirm deletion
        confirm_btn = page.locator(
            '[role="alertdialog"] button:has-text("Delete"), '
            '[role="dialog"] button.artdeco-button--primary:has-text("Delete")'
        )
        if await confirm_btn.count() == 0:
            await save_cookies(page)
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Confirmation dialog did not appear"}))]

        await confirm_btn.first.click()
        await page.wait_for_timeout(3000)
        logger.info(f"Deleted post: {post_url}")
        await save_cookies(page)

        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "action": "delete_post",
            "message": f"Post deleted: {post_url}"
        }))]


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8988
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
    )

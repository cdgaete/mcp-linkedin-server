from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from playwright.async_api import async_playwright
import asyncio
import os
import json
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import time
import logging
import sys
from pathlib import Path

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

# Load environment variables
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)
    logger.info(f"Loaded environment from {env_path}")

# Create MCP server
server = Server("linkedin-browser")


def setup_sessions_directory():
    sessions_dir = Path(__file__).parent / 'sessions'
    sessions_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
    return sessions_dir


async def save_cookies(page, platform):
    """Save cookies to encrypted file"""
    cookies = await page.context.cookies()
    cookie_data = {"timestamp": int(time.time()), "cookies": cookies}

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

    cookie_file = sessions_dir / f'{platform}_cookies.json'
    with open(cookie_file, 'wb') as f:
        f.write(encrypted)
    logger.info("Cookies saved")


async def load_cookies(context, platform):
    """Load cookies from encrypted file"""
    sessions_dir = Path(__file__).parent / 'sessions'
    cookie_file = sessions_dir / f'{platform}_cookies.json'
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

        # Check expiration (24 hours)
        if time.time() - cookie_data["timestamp"] > 86400:
            cookie_file.unlink()
            return False

        await context.add_cookies(cookie_data["cookies"])
        logger.info("Cookies loaded")
        return True
    except Exception as e:
        logger.warning(f"Failed to load cookies: {e}")
        return False


class BrowserSession:
    def __init__(self, headless=True):
        self.headless = headless
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
        await load_cookies(self.context, 'linkedin')
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


# Define tools
@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="login_linkedin",
            description="Open LinkedIn login page in browser for manual login.",
            inputSchema={"type": "object", "properties": {}, "required": []}
        ),
        Tool(
            name="browse_linkedin_feed",
            description="Browse LinkedIn feed and return recent posts",
            inputSchema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 5, "description": "Number of posts to retrieve"}},
                "required": []
            }
        ),
        Tool(
            name="search_linkedin_profiles",
            description="Search for LinkedIn profiles matching a query",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "count": {"type": "integer", "default": 5}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="search_linkedin_posts",
            description="Search for LinkedIn posts by keywords",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "count": {"type": "integer", "default": 5}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="view_linkedin_profile",
            description="Visit and extract data from a LinkedIn profile URL",
            inputSchema={
                "type": "object",
                "properties": {"profile_url": {"type": "string", "description": "LinkedIn profile URL"}},
                "required": ["profile_url"]
            }
        ),
        Tool(
            name="interact_with_linkedin_post",
            description="Interact with a LinkedIn post (like, comment, or read)",
            inputSchema={
                "type": "object",
                "properties": {
                    "post_url": {"type": "string", "description": "LinkedIn post URL"},
                    "action": {"type": "string", "enum": ["like", "comment", "read"], "default": "read"},
                    "comment": {"type": "string", "description": "Comment text (required if action is 'comment')"}
                },
                "required": ["post_url"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "login_linkedin":
            return await do_login()
        elif name == "browse_linkedin_feed":
            return await do_browse_feed(arguments.get("count", 5))
        elif name == "search_linkedin_profiles":
            return await do_search_profiles(arguments["query"], arguments.get("count", 5))
        elif name == "search_linkedin_posts":
            return await do_search_posts(arguments["query"], arguments.get("count", 5))
        elif name == "view_linkedin_profile":
            return await do_view_profile(arguments["profile_url"])
        elif name == "interact_with_linkedin_post":
            return await do_interact_post(
                arguments["post_url"],
                arguments.get("action", "read"),
                arguments.get("comment")
            )
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.error(f"Tool error: {e}")
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": str(e)}))]


async def do_login():
    """Login to LinkedIn"""
    username = os.getenv('LINKEDIN_USERNAME', '').strip()
    password = os.getenv('LINKEDIN_PASSWORD', '').strip()

    async with BrowserSession(headless=False) as session:
        page = await session.new_page()
        await page.goto('https://www.linkedin.com/login', wait_until='networkidle')

        if 'feed' in page.url:
            await save_cookies(page, 'linkedin')
            return [TextContent(type="text", text=json.dumps({"status": "success", "message": "Already logged in"}))]

        if username:
            await page.fill('#username', username)
        if password:
            await page.fill('#password', password)

        logger.info("Waiting for manual login (5 min timeout)...")

        try:
            await page.wait_for_url('**/feed/**', timeout=300000)
            await save_cookies(page, 'linkedin')
            await asyncio.sleep(2)
            return [TextContent(type="text", text=json.dumps({"status": "success", "message": "Login successful"}))]
        except:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Login timeout"}))]


async def do_browse_feed(count: int):
    """Browse LinkedIn feed - Using discovered selectors Dec 2025"""
    posts = []

    async with BrowserSession(headless=False) as session:
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

        await save_cookies(page, 'linkedin')
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "posts": posts[:count],
            "total_found": len(posts)
        }, indent=2))]


async def do_search_profiles(query: str, count: int):
    """Search for LinkedIn profiles - Updated selectors"""
    import urllib.parse
    encoded_query = urllib.parse.quote(query)

    async with BrowserSession(headless=False) as session:
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

        await save_cookies(page, 'linkedin')
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "profiles": profiles,
            "count": len(profiles),
            "query": query
        }, indent=2))]


async def do_search_posts(query: str, count: int):
    """Search for LinkedIn posts by keywords"""
    import urllib.parse
    encoded_query = urllib.parse.quote(query)
    
    async with BrowserSession(headless=False) as session:
        # LinkedIn post search URL
        page = await session.new_page(f'https://www.linkedin.com/search/results/content/?keywords={encoded_query}')
        
        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]
        
        await page.wait_for_timeout(5000)
        
        posts = []
        for scroll_attempt in range(min(count + 2, 10)):
            new_posts = await page.evaluate('''() => {
                const posts = [];
                
                // Find posts in search results
                const containers = document.querySelectorAll('div.feed-shared-update-v2[data-urn^="urn:li:activity"]');
                
                containers.forEach(container => {
                    try {
                        const urn = container.getAttribute('data-urn');
                        if (!urn) return;
                        
                        // Author
                        const authorEl = container.querySelector('.update-components-actor__title span');
                        const author = authorEl ? authorEl.innerText.trim().split('\\n')[0] : 'Unknown';
                        
                        // Date
                        const dateEl = container.querySelector('.update-components-actor__sub-description');
                        const date = dateEl ? dateEl.innerText.trim().split('\\n')[0] : '';
                        
                        // Content
                        const contentEl = container.querySelector(
                            '.feed-shared-inline-show-more-text span[dir="ltr"], ' +
                            '.feed-shared-update-v2__description span[dir="ltr"]'
                        );
                        const content = contentEl ? contentEl.innerText.trim().substring(0, 1000) : '';
                        
                        // Reactions
                        const reactionsEl = container.querySelector('.social-details-social-counts__reactions-count');
                        const reactions = reactionsEl ? reactionsEl.innerText.trim() : '0';
                        
                        // Comments
                        const commentsEl = container.querySelector('.social-details-social-counts__comments');
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
        
        await save_cookies(page, 'linkedin')
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "posts": posts[:count],
            "total_found": len(posts),
            "query": query
        }, indent=2))]


async def do_view_profile(profile_url: str):
    """View a LinkedIn profile - Updated selectors"""
    if 'linkedin.com/in/' not in profile_url:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Invalid LinkedIn profile URL"}))]

    async with BrowserSession(headless=False) as session:
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

        await save_cookies(page, 'linkedin')
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "profile": profile,
            "url": profile_url
        }, indent=2))]


async def do_interact_post(post_url: str, action: str, comment: str = None):
    """Read a LinkedIn post with comments"""
    if 'linkedin.com/posts/' not in post_url and 'linkedin.com/feed/update/' not in post_url:
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Invalid LinkedIn post URL"}))]

    async with BrowserSession(headless=False) as session:
        page = await session.new_page(post_url)

        if 'login' in page.url:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": "Not logged in"}))]

        # Wait for post to load
        await page.wait_for_timeout(5000)

        # Click to load comments if there's a comments button
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

        await save_cookies(page, 'linkedin')
        return [TextContent(type="text", text=json.dumps({
            "status": "success",
            "action": action,
            "post": post_data["post"],
            "comments": post_data["comments"],
            "comments_found": len(post_data["comments"])
        }, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

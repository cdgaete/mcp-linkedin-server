# LinkedIn Browser MCP Server

A FastMCP-based server for LinkedIn automation and data extraction using browser automation. This server provides a set of tools for interacting with LinkedIn programmatically while respecting LinkedIn's terms of service and rate limits.

## Features

- **Secure Authentication**
  - Environment-based credential management
  - Session persistence with encrypted cookie storage
  - Rate limiting protection
  - Automatic session recovery

- **Profile Operations**
  - View and extract profile information
  - Search for profiles based on keywords
  - Browse LinkedIn feed
  - Profile visiting capabilities

- **Post Interactions**
  - Like posts
  - Comment on posts
  - Read post content and engagement metrics

## Prerequisites

- Python 3.8+
- Playwright
- FastMCP library
- LinkedIn account

## Installation

1. Clone the repository:
```bash
git clone [repository-url]
cd mcp-linkedin-server
```

2. Create and activate a virtual environment:
```bash
python -m venv env
source env/bin/activate  # On Windows: env\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

4. Set up environment variables:
Create a `.env` file in the root directory with:
```env
LINKEDIN_USERNAME=your_email@example.com
LINKEDIN_PASSWORD=your_password
COOKIE_ENCRYPTION_KEY=your_encryption_key  # Optional: will be auto-generated if not provided
```

## Usage

1. Start the MCP server:
```bash
python linkedin_browser_mcp.py
```

2. Available Tools:

- `login_linkedin_secure`: Securely log in using environment credentials
- `browse_linkedin_feed`: Browse and extract posts from feed
- `search_linkedin_profiles`: Search for profiles matching criteria
- `view_linkedin_profile`: View and extract data from specific profiles
- `interact_with_linkedin_post`: Like, comment, or read posts

### Example Usage

```python
from fastmcp import FastMCP

# Initialize client
client = FastMCP.connect("http://localhost:8000")

# Login
result = await client.login_linkedin_secure()
print(result)

# Search profiles
profiles = await client.search_linkedin_profiles(
    query="software engineer",
    count=5
)
print(profiles)

# View profile
profile_data = await client.view_linkedin_profile(
    profile_url="https://www.linkedin.com/in/username"
)
print(profile_data)
```

## Security Features

- Encrypted cookie storage
- Rate limiting protection
- Secure credential management
- Session persistence
- Browser automation security measures

## Best Practices

1. **Rate Limiting**: The server implements rate limiting to prevent excessive requests:
   - Maximum 5 login attempts per hour
   - Automatic session reuse
   - Cookie persistence to minimize login needs

2. **Error Handling**: Comprehensive error handling for:
   - Network issues
   - Authentication failures
   - LinkedIn security challenges
   - Invalid URLs or parameters

3. **Session Management**:
   - Automatic cookie encryption
   - Session persistence
   - Secure storage practices

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

MIT

## Disclaimer

This tool is for educational purposes only. Ensure compliance with LinkedIn's terms of service and rate limiting guidelines when using this software. 
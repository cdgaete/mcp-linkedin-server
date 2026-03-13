# LinkedIn MCP — Maintenance Guide

LinkedIn continuously obfuscates its frontend. CSS class names are replaced with
random hashes on every deploy, and DOM structure changes without notice. When this
happens the `querySelectorAll` calls inside `do_search_posts` and `do_browse_feed`
stop matching anything and the tools return empty results.

---

## Signs the MCP is broken

- `search_linkedin_posts` returns `[]` with `total_found: 0`
- `browse_linkedin_feed` returns `[]`
- A JS `SyntaxError` or `TypeError` appears in MCP logs
- The `_debug` key appears in results with all counts at `0`

---

## Diagnostic toolkit — `diagnose.py`

Four commands, always run from the repo root:

```bash
# 1. Dump live DOM structure of a search results page (most useful)
DISPLAY=:0 .venv/bin/python3 diagnose.py search "life cycle assessment"

# 2. Dump live DOM structure of the feed page
DISPLAY=:0 .venv/bin/python3 diagnose.py feed

# 3. List all CSS selectors currently in linkedin_browser_mcp.py
.venv/bin/python3 diagnose.py selectors

# 4. Syntax-check every JS evaluate() block with node
.venv/bin/python3 diagnose.py validate
```

Commands 3 and 4 need no browser. Commands 1 and 2 require `DISPLAY=:0`
(Xvfb or a real display) and a valid session in `sessions/`.

---

## Step-by-step repair procedure

### 1 — Confirm it is a selector problem

```bash
DISPLAY=:0 .venv/bin/python3 diagnose.py search "life cycle assessment"
```

Look at the `strategies` block in the output. It shows how many elements each
selector strategy currently finds on the live page. If all are `0`, LinkedIn
changed the DOM and selectors need updating.

### 2 — Identify the new container selector

The output includes `view_names` (all `data-view-name` values on the page)
and `componentkey_samples` (first 20 `componentkey` attribute values).

LinkedIn's `data-view-name` attributes are more stable than class names.
Key values to look for:

| `data-view-name` value | What it is |
|---|---|
| `feed-full-update` | Post card container |
| `feed-commentary` | Post body text |
| `feed-reaction-count` | Reaction count element |
| `feed-comment-count` | Comment count element |
| `search-entity-result-universal-template` | Search result wrapper |

If none of these appear in `view_names`, LinkedIn made a deeper structural change
and you will need to inspect `first_post.children` to find new stable attributes.

### 3 — Inspect the first real post container

`first_post.children` in the `search` output lists every `a`, `time`,
`[dir=ltr]`, `[aria-label]`, and `[data-view-name]` child of the first matched
post. Use this to find:

| What | Where to look |
|---|---|
| Author name | `a[href*="/in/"]` with non-empty `text` |
| Post content | element with `data-view-name: feed-commentary` or `dir: ltr` |
| Reactions | element with `data-view-name: feed-reaction-count` |
| Comments | element with `data-view-name: feed-comment-count` |
| Date | element whose `text` matches `1mo`, `3d`, `2w` etc., or a `time[datetime]` |
| Post URL | `a[href*="/feed/update/"]` — if absent, LinkedIn is JS-routing and URLs are unavailable |

### 4 — Edit `linkedin_browser_mcp.py`

The two functions to fix are:

- `do_search_posts` (search for `def do_search_posts`)
- `do_browse_feed` (search for `def do_browse_feed`)

Update the `querySelectorAll` calls inside their `page.evaluate()` blocks to
match what the dump showed.

**Escaping rule — the most common source of broken JS:**

All JS lives inside Python triple-quoted strings. Backslashes must be doubled:

| You want in JS | Write in Python string |
|---|---|
| `split('\n')` | `split('\\n')` |
| `/\d+/` | `/\\d+/` |
| `'string'` | fine inside `"""` blocks |

Always use `"""` (double triple-quote) as the evaluate delimiter so single
quotes inside JS don't need escaping.

### 5 — Validate before restarting

```bash
# Check JS syntax
.venv/bin/python3 diagnose.py validate

# Check Python syntax
python3 -c "import ast; ast.parse(open('linkedin_browser_mcp.py').read()); print('OK')"
```

Both must pass before restarting.

### 6 — Restart the MCP server

The MCP server is a stdio process managed by the MCP client (Claude Desktop or
Cursor). To reload changes, toggle the MCP connection off and on in the client's
settings. The server will restart automatically on reconnect.

Find the current PID if needed:
```bash
ps aux | grep linkedin_browser_mcp | grep -v grep
```

### 7 — Test

```bash
# Quick functional test via the MCP tool (after reconnecting):
search_linkedin_posts(query="life cycle assessment", count=3)
```

Expected: results with `author`, `date`, `content`, `reactions`, `comments`
populated, no `_debug` key, no duplicates.

---

## Change log

| Date | What changed | Fix applied |
|---|---|---|
| 2026-03 | All CSS class names obfuscated to hashes; `data-urn` removed from search containers | Switched to `[componentkey]` filtered by profile link + text length; `data-view-name` attributes for field extraction |
| 2026-03 | All automation tools were running `headless=False`, blocking headless server deployment | `do_browse_feed`, `do_search_profiles`, `do_search_posts`, `do_view_profile`, `do_interact_post` changed to `headless=True`. Only `do_login` keeps `headless=False` — login always requires a visible browser for manual 2FA. |

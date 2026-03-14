#!/bin/bash
echo "=== RESTART $(date) ===" >> /tmp/linkedin-mcp.log
cd /home/carlos/repos/mcp-linkedin-server
exec .venv/bin/python -u linkedin_browser_mcp.py --http Si9MvLyQa6OuLSUQPeej1InnoC_Xxfa4M9pJjmt-51E --port 8988 >> /tmp/linkedin-mcp.log 2>&1

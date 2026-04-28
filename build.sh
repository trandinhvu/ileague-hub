#!/bin/bash
# ============================================================
# Build iLeague Hub Agent → standalone executable
# Usage: bash build.sh
# Output: dist/iLeagueHub (Mac) or dist/iLeagueHub.exe (Windows)
# ============================================================

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "📦 Installing dependencies..."
pip install -r requirements.txt pyinstaller 2>/dev/null || pip install --break-system-packages -r requirements.txt pyinstaller

echo "🔨 Building iLeague Hub..."
pyinstaller \
    --name iLeagueHub \
    --onefile \
    --noconsole \
    --add-data "static:static" \
    --hidden-import pystray \
    --hidden-import PIL \
    --hidden-import PIL.ImageDraw \
    hub_agent.py

echo ""
echo "✅ Build complete!"
echo "   Output: $DIR/dist/iLeagueHub"
echo ""
echo "📁 To create distributable ZIP:"
echo "   cd dist && zip -r iLeagueHub.zip iLeagueHub"

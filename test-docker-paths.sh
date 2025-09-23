#!/bin/bash

echo "🧪 Testing Docker paths and permissions..."

# Test if container is running
if ! docker ps | grep -q discord-music-bot; then
    echo "❌ Container not running. Start it first with: docker-compose up -d"
    exit 1
fi

echo "📁 Checking Docker volume mounts..."
docker exec discord-music-bot ls -la /app/temp/
echo ""

echo "📁 Checking temp directory permissions..."
docker exec discord-music-bot ls -la /tmp/
echo ""

echo "🔍 Checking environment variables..."
docker exec discord-music-bot printenv | grep TEMP
echo ""

echo "💾 Testing file creation in temp directory..."
docker exec discord-music-bot touch /app/temp/test-file.txt
docker exec discord-music-bot ls -la /app/temp/test-file.txt
docker exec discord-music-bot rm /app/temp/test-file.txt
echo ""

echo "🎵 Checking FFmpeg availability..."
docker exec discord-music-bot ffmpeg -version | head -1
echo ""

echo "✅ Docker path testing complete!"
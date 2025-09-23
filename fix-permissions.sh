#!/bin/bash

echo "🔧 Fixing Docker permissions for Discord bot..."

# Ensure directories exist
mkdir -p ./temp ./logs

# Fix host permissions (accessible from Docker)
chmod 777 ./temp ./logs

# Show current permissions
echo "📋 Current permissions:"
ls -la ./temp ./logs

# If container is running, check internal permissions
if docker ps | grep -q discord-music-bot; then
    echo ""
    echo "🐳 Checking Docker internal permissions..."
    docker exec discord-music-bot ls -la /app/temp/ /app/logs/

    echo ""
    echo "🧪 Testing Docker write permissions..."
    docker exec discord-music-bot touch /app/temp/test-write.tmp
    if [ $? -eq 0 ]; then
        echo "✅ Docker can write to temp directory"
        docker exec discord-music-bot rm -f /app/temp/test-write.tmp
    else
        echo "❌ Docker cannot write to temp directory"
    fi
fi

echo ""
echo "✅ Permission fix complete!"
echo "💡 If issues persist, rebuild container: docker-compose up --build -d"
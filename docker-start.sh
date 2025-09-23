#!/bin/bash

echo "🐳 Starting Discord Bot with Docker optimizations..."

# Set environment for better Discord connectivity
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

# Ensure temp directory exists and has proper permissions
mkdir -p /app/temp
chmod 755 /app/temp

# Show network configuration
echo "🌐 Network configuration:"
ip route | head -3
echo ""

# Show DNS configuration
echo "🔍 DNS configuration:"
cat /etc/resolv.conf | head -5
echo ""

# Test Discord connectivity
echo "🔗 Testing Discord connectivity..."
nslookup discord.com | head -5
echo ""

# Start the bot
echo "🚀 Starting Discord Music Bot..."
exec python bot.py
#!/bin/bash

# Docker Network Optimization Script for Discord Voice
echo "🔧 Optimizing Docker for Discord voice connections..."

# Stop existing containers
echo "📦 Stopping existing containers..."
docker-compose down

# Clean up Docker networks
echo "🧹 Cleaning up Docker networks..."
docker network prune -f

# Optimize Docker daemon settings
echo "⚙️ Checking Docker daemon configuration..."
if [ -f /etc/docker/daemon.json ]; then
    echo "📋 Current daemon.json exists"
else
    echo "📝 Creating optimized daemon.json..."
    sudo tee /etc/docker/daemon.json > /dev/null <<EOF
{
  "dns": ["8.8.8.8", "1.1.1.1"],
  "mtu": 1500,
  "userland-proxy": false,
  "experimental": false,
  "features": {
    "buildkit": true
  }
}
EOF
fi

# Restart Docker daemon
echo "🔄 Restarting Docker daemon..."
sudo systemctl restart docker

# Wait for Docker to be ready
echo "⏳ Waiting for Docker to be ready..."
sleep 5

# Rebuild and start
echo "🚀 Building and starting optimized container..."
docker-compose up --build -d

echo "✅ Docker optimization complete!"
echo "📋 Check logs with: docker-compose logs -f"
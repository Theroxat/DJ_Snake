# 🎵 Discord Music Bot

A powerful and easy-to-use Discord bot that plays music from YouTube in voice channels. Built with discord.py and yt-dlp for reliable music streaming.

[![CI/CD Pipeline](https://github.com/Theroxat/DJ_Snake/workflows/CI/CD%20Pipeline/badge.svg)](https://github.com/Theroxat/DJ_Snake/actions)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## ✨ Features

- 🎶 **High-quality music streaming** from YouTube using yt-dlp
- 📋 **Playlist support** with automatic queue management (up to 30 tracks by default)
- 🔧 **Robust voice connection** system with error recovery
- 🌐 **Multi-server support** - each server has its own independent queue
- 🛡️ **Security-focused** with environment variable configuration
- 🐳 **Docker support** for easy deployment
- 🔄 **Auto-cleanup** of temporary files and voice connections
- ⚡ **Fast and responsive** with asynchronous processing

## 🚀 Quick Start

### Option 1: Standard Installation

1. **Clone the repository**
```bash
git clone https://github.com/Theroxat/DJ_Snake.git
cd DJ_Snake
```

2. **Install dependencies**
```bash
# Install FFmpeg (required)
sudo apt install ffmpeg  # Ubuntu/Debian
brew install ffmpeg      # macOS

# Install Python dependencies
pip install -r requirements.txt
```

3. **Configure the bot**
```bash
cp .env.example .env
# Edit .env and add your Discord bot token
```

4. **Run the bot**
```bash
python bot.py
```

### Option 2: Docker Deployment (Recommended)

1. **Clone and configure**
```bash
git clone https://github.com/Theroxat/DJ_Snake.git
cd DJ_Snake
cp .env.example .env
# Edit .env with your bot token
```

2. **Run with Docker Compose**
```bash
docker-compose up -d
```

📖 **For detailed setup instructions, see [INSTALLATION.md](INSTALLATION.md)**

## 🎮 Commands

| Command | Description | Example |
|---------|-------------|---------|
| `!play <URL>` | Play music from YouTube URL or search | `!play https://youtu.be/dQw4w9WgXcQ` |
| `!list` | Show current queue | `!list` |
| `!next` | Skip to next track | `!next` |
| `!stop` | Stop playback and clear queue | `!stop` |
| `!reset` | Reset voice connection | `!reset` |

## 🔧 Configuration

### Environment Variables

Create a `.env` file with the following variables:

```env
# Required
DISCORD_TOKEN=your_bot_token_here

# Optional
COMMAND_PREFIX=!
MAX_PLAYLIST_SIZE=30
AUDIO_FORMAT=bestaudio/best
```

### Discord Bot Setup

1. Create a bot at [Discord Developer Portal](https://discord.com/developers/applications)
2. Enable these **Privileged Gateway Intents**:
   - Message Content Intent ✅
   - Voice State Intent ✅
3. Bot permissions needed:
   - Send Messages
   - Connect to Voice Channels
   - Speak in Voice Channels
   - Use Voice Activity

## 🐳 Docker Deployment

### Using Docker Compose (Recommended)

```bash
# Clone the repository
git clone https://github.com/Theroxat/DJ_Snake.git
cd DJ_Snake

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Start the bot
docker-compose up -d

# View logs
docker-compose logs -f discord-bot

# Stop the bot
docker-compose down
```

### Using Docker directly

```bash
# Build the image
docker build -t discord-music-bot .

# Run the container
docker run -d \
  --name discord-music-bot \
  --env-file .env \
  --restart unless-stopped \
  discord-music-bot
```

## 🛠️ Development

### Setting up Development Environment

```bash
# Clone the repository
git clone https://github.com/Theroxat/DJ_Snake.git
cd DJ_Snake

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate     # Windows

# Install dependencies including dev tools
pip install -r requirements.txt
pip install pytest black flake8

# Test the bot
python bot.py
```

### Code Quality

```bash
# Format code
black .

# Lint code
flake8 .

# Run security checks
bandit -r .
```

## 🏗️ Architecture

- **MusicPlayer Class**: Manages per-server queues and playback state
- **Voice Connection System**: Robust connection handling with error recovery
- **Download System**: Uses yt-dlp with fallback strategies
- **Queue Management**: Automatic file cleanup and session management

For detailed architecture information, see [CLAUDE.md](CLAUDE.md).

## 🔒 Security

- ✅ **Token protection** via environment variables
- ✅ **Input validation** for URLs and commands
- ✅ **Resource limits** to prevent abuse
- ✅ **No credential storage** in code
- ✅ **Docker security** with non-root user

## 🚨 Troubleshooting

### Common Issues

**Bot doesn't respond to commands**
- Ensure Message Content Intent is enabled
- Check bot permissions in the server
- Verify the bot is online and connected

**"FFmpeg not found" error**
- Install FFmpeg: `sudo apt install ffmpeg` (Linux) or `brew install ffmpeg` (macOS)
- On Windows, add FFmpeg to your PATH

**Voice connection issues**
- Try the `!reset` command
- Ensure the bot has Connect and Speak permissions
- Check if the voice channel is full

**YouTube download errors**
- Update yt-dlp: `pip install --upgrade yt-dlp`
- Some videos may be region-locked or age-restricted

## 📋 Requirements

- **Python 3.8+**
- **FFmpeg** (for audio processing)
- **Discord Bot Token**
- **Stable internet connection**

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guidelines](CONTRIBUTING.md) for details.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [discord.py](https://github.com/Rapptz/discord.py) - Discord API wrapper
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - YouTube downloader
- [FFmpeg](https://ffmpeg.org/) - Audio processing

## 📞 Support

- **Documentation**: [Installation Guide](INSTALLATION.md)
- **Issues**: [GitHub Issues](https://github.com/Theroxat/DJ_Snake/issues)
- **Discussions**: [GitHub Discussions](https://github.com/Theroxat/DJ_Snake/discussions)

---

**⭐ If you find this project useful, please consider giving it a star!**
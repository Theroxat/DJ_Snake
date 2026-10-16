# Installation Guide

This guide will help you set up the Discord Music Bot on your system.

## Prerequisites

Before you begin, ensure you have the following installed:

- **Python 3.8 or higher**
- **FFmpeg** (required for audio processing)
- **Git** (for cloning the repository)

## Step 1: Install System Dependencies

### FFmpeg Installation

#### Windows
1. Download FFmpeg from [https://ffmpeg.org/download.html](https://ffmpeg.org/download.html)
2. Extract the archive to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to your system PATH

#### macOS
```bash
# Using Homebrew
brew install ffmpeg
```

#### Linux (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install ffmpeg
```

#### Linux (CentOS/RHEL/Fedora)
```bash
# CentOS/RHEL
sudo yum install ffmpeg
# or Fedora
sudo dnf install ffmpeg
```

## Step 2: Clone the Repository

```bash
git clone https://github.com/yourusername/discord-music-bot.git
cd discord-music-bot
```

## Step 3: Set Up Python Environment

### Create Virtual Environment (Recommended)
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate
```

### Install Python Dependencies
```bash
pip install -r requirements.txt
```

## Step 4: Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application" and give it a name
3. Go to the "Bot" section in the left sidebar
4. Click "Add Bot"
5. Copy the bot token (you'll need this later)
6. Under "Privileged Gateway Intents", enable:
   - **Message Content Intent**
   - **Server Members Intent** (optional)

## Step 5: Invite Bot to Your Server

1. In the Discord Developer Portal, go to "OAuth2" > "URL Generator"
2. Select scopes: `bot`
3. Select bot permissions:
   - Send Messages
   - Connect
   - Speak
   - Use Voice Activity
4. Copy the generated URL and open it in your browser
5. Select your server and authorize the bot

## Step 6: Configure the Bot

### Method 1: Environment Variables (Recommended)

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Edit `.env` file and add your bot token:
```env
DISCORD_TOKEN=your_bot_token_here
COMMAND_PREFIX=!
MAX_PLAYLIST_SIZE=30
SESSION_IDLE_HOURS=6
```

### Method 2: Token File (Less Secure)

Create a file named `token` (no extension) and paste your bot token:
```bash
echo "your_bot_token_here" > token
```

## Step 7: Run the Bot

```bash
python bot.py
```

You should see output similar to:
```
Starting Discord Music Bot (fixed version)
--------------------------------------------------
BotName#1234 is connected and ready!
Bot ID: 123456789012345678
Connected to 1 guilds
```

## Testing the Installation

1. Start the bot with `python bot.py`
2. Join a voice channel in your Discord server
3. In a text channel, type: `!play https://www.youtube.com/watch?v=dQw4w9WgXcQ`
4. The bot should join your voice channel and start playing music

## Available Commands

- `!play <URL>` - Play music from YouTube
- `!list` - Show current queue
- `!next` - Skip to next track
- `!pause` - Pause playback
- `!resume` - Resume playback
- `!stop` - Stop playback and clear queue
- `!reset` - Reset voice connection

## Troubleshooting

### Common Issues

#### "FFmpeg not found"
- Make sure FFmpeg is installed and in your system PATH
- Test with: `ffmpeg -version`

#### "Bot doesn't respond"
- Check that Message Content Intent is enabled
- Verify the bot has permission to read messages in the channel

#### "Can't connect to voice channel"
- Ensure the bot has Connect and Speak permissions
- Try the `!reset` command

#### "yt-dlp errors"
- Update yt-dlp: `pip install --upgrade yt-dlp`
- Some videos may be region-locked or unavailable

#### "Import errors"
- Ensure all dependencies are installed: `pip install -r requirements.txt`
- Check Python version compatibility (3.8+)

### Getting Help

If you encounter issues:
1. Check the console output for error messages
2. Ensure all dependencies are correctly installed
3. Verify your bot token is correct
4. Check Discord permissions

## Security Notes

- Never share your bot token publicly
- Use environment variables instead of token files when possible
- Add `token` and `.env` files to `.gitignore`
- Regenerate your token if it's accidentally exposed

## Advanced Configuration

### Custom Prefix
Set `COMMAND_PREFIX` in your `.env` file:
```env
COMMAND_PREFIX=$
```

### Playlist Size Limit
Adjust `MAX_PLAYLIST_SIZE` to control how many tracks can be added from playlists:
```env
MAX_PLAYLIST_SIZE=50
```

### Media Quality
Tracks are downloaded as one mp4 (video for the web page, audio for Discord).
`MEDIA_FORMAT` (it replaces the old `AUDIO_FORMAT`, which is no longer read)
picks the yt-dlp format; keep it browser-playable, for example:
```env
MEDIA_FORMAT=best[height<=480][ext=mp4]/best
```
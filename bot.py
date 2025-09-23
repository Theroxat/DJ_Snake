import os
import asyncio
import yt_dlp
import discord
from discord.ext import commands
import tempfile
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure intents with all necessary permissions
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.guild_messages = True

# Configuration from environment variables
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
MAX_PLAYLIST_SIZE = int(os.getenv("MAX_PLAYLIST_SIZE", "30"))
AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "bestaudio/best")
TEMP_DIR = os.getenv("TEMP_DIR", tempfile.mkdtemp())

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
temp_dir = TEMP_DIR


class MusicPlayer:
    def __init__(self):
        self.voice_client = None
        self.queue = []
        self.current_file = None
        self.is_playing = False
        self.should_stop = False
        self.now_playing = None

    def cleanup(self):
        if self.current_file and os.path.exists(self.current_file):
            try:
                os.remove(self.current_file)
            except:
                pass
            self.current_file = None

    def add_to_queue(self, filepath, title):
        if not self.should_stop:
            self.queue.append({"filepath": filepath, "title": title})

    def next_in_queue(self):
        if self.queue:
            return self.queue.pop(0)
        return None

    def stop_and_clear(self):
        self.should_stop = True
        self.queue.clear()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.cleanup()
        self.is_playing = False

    async def disconnect_voice_client(self):
        """Safely disconnect and cleanup voice client"""
        if self.voice_client:
            try:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect()
            except Exception as e:
                print(f"Error during voice client disconnect: {e}")
            finally:
                self.voice_client = None


music_players = {}


def get_playlist_info(url):
    """Extract playlist information without downloading."""
    try:
        print(f"Extracting playlist from: {url}")

        # Configuration to extract playlist
        ydl_opts = {
            "extract_flat": "in_playlist",  # Important: use 'in_playlist' for playlists
            "quiet": True,  # Reduce log spam
            "no_warnings": True,
            "ignoreerrors": True,  # Continue even if some videos have errors
            "playlistend": MAX_PLAYLIST_SIZE,  # Limit based on config
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                print("No information extracted")
                return []

            # Debug: display the type of info received
            info_type = info.get("_type", "video")
            print(f"Info type: {info_type}")
            print(f"Title: {info.get('title', 'Not found')}")

            # Handle different types of results
            if info_type == "playlist" or "entries" in info:
                entries = info.get("entries", [])

                if entries:
                    print(f"Number of entries found: {len(entries)}")

                    # Filter None entries and extract necessary info
                    valid_entries = []
                    for entry in entries:
                        if entry is not None:
                            processed_entry = {
                                "id": entry.get("id"),
                                "title": entry.get("title", "Unknown title"),
                                "url": entry.get("url")
                                or entry.get("webpage_url")
                                or f"https://www.youtube.com/watch?v={entry.get('id')}",
                                "webpage_url": entry.get("webpage_url")
                                or entry.get("url"),
                            }
                            if processed_entry["id"]:
                                valid_entries.append(processed_entry)

                    print(f"Number of valid entries: {len(valid_entries)}")
                    return valid_entries
                else:
                    print("Empty playlist or not found")
                    return []
            else:
                # It's a single video, not a playlist
                print("URL detected as single video, not a playlist")
                return []

    except Exception as e:
        print(f"Error extracting playlist: {e}")
        return []


# Simplified voice connection function to avoid error 4006
async def safe_voice_connect(voice_channel, max_attempts=3):
    """Secure voice connection that avoids error 4006"""
    guild = voice_channel.guild

    # Check if we already have a working connection to the same channel
    if guild.voice_client and guild.voice_client.is_connected():
        if guild.voice_client.channel == voice_channel:
            print(f"✅ Already connected to {voice_channel.name}")
            return guild.voice_client
        else:
            print(
                f"🔄 Switching from {guild.voice_client.channel.name} to {voice_channel.name}"
            )
            await guild.voice_client.disconnect(force=True)
            await asyncio.sleep(2)
    elif guild.voice_client:
        print(f"🔄 Cleaning up disconnected voice client")
        await guild.voice_client.disconnect(force=True)
        await asyncio.sleep(2)

    for attempt in range(max_attempts):
        try:
            print(
                f"🔌 Attempt {attempt + 1}/{max_attempts} to connect to {voice_channel.name}"
            )

            # Wait between attempts to avoid error 4006 and WebSocket issues
            if attempt > 0:
                wait_time = 5 + (attempt * 2)  # Longer waits for WebSocket stability
                print(f"⏳ Waiting {wait_time} seconds...")
                await asyncio.sleep(wait_time)

            # Connection with optimal parameters for Docker/WebSocket
            voice_client = await voice_channel.connect(
                timeout=90.0,  # Longer timeout for Docker
                reconnect=False,  # No auto reconnect to avoid loops
                self_deaf=True,  # Mute ourselves
                self_mute=False,  # But can speak (play music)
            )

            # Check that the connection works
            if voice_client and voice_client.is_connected():
                print(f"✅ Successfully connected to {voice_channel.name}")
                return voice_client
            else:
                print("⚠️ Connection established but not confirmed")
                if voice_client:
                    await voice_client.disconnect()

        except discord.errors.ConnectionClosed as e:
            if e.code == 4006:
                print(f"❌ Error 4006 detected (attempt {attempt + 1})")
                # For 4006, wait longer and clean completely
                await asyncio.sleep(5)
            else:
                print(f"❌ Connection error {e.code}: {e.reason}")

        except discord.ClientException as e:
            if "already connected" in str(e).lower():
                # If already connected, find and return the connection
                if guild.voice_client and guild.voice_client.channel == voice_channel:
                    print("✅ Existing connection found and reused")
                    return guild.voice_client
            print(f"❌ Client error: {e}")

        except Exception as e:
            error_msg = str(e)
            if "522" in error_msg or "Invalid response status" in error_msg:
                print(f"🌐 WebSocket error (attempt {attempt + 1}): {e}")
                print("💡 This is often caused by network connectivity issues")
            else:
                print(f"❌ Unexpected error: {e}")

    print("❌ All connection attempts failed")
    return None


async def join_voice_channel(ctx):
    """Simplified voice connection function"""
    if not ctx.author.voice:
        await ctx.send("❌ You must be in a voice channel!")
        return False

    voice_channel = ctx.author.voice.channel

    # Initialize player if necessary
    if ctx.guild.id not in music_players:
        music_players[ctx.guild.id] = MusicPlayer()

    player = music_players[ctx.guild.id]

    # Use our secure connection function
    voice_client = await safe_voice_connect(voice_channel)

    if voice_client:
        player.voice_client = voice_client
        return True
    else:
        await ctx.send("❌ Unable to connect to voice channel.")
        return False


def download_audio(url):
    try:
        # Ensure temp directory exists
        os.makedirs(temp_dir, exist_ok=True)

        # Simplified but robust configuration
        ydl_opts = {
            "format": AUDIO_FORMAT,
            "noplaylist": True,
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "quiet": False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Downloading: {url}")
            info = ydl.extract_info(url, download=True)

            video_id = info.get("id", "")
            title = info.get("title", "Unknown")

            # Find the downloaded file
            temp_files = os.listdir(temp_dir)

            for f in temp_files:
                if video_id in f:
                    filepath = os.path.join(temp_dir, f)
                    # Verify file exists and has content
                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        return filepath, title

            # If not found by ID, take the most recent
            if temp_files:
                import time

                latest_file = max(
                    temp_files,
                    key=lambda f: os.path.getctime(os.path.join(temp_dir, f)),
                )
                filepath = os.path.join(temp_dir, latest_file)
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    return filepath, title

            raise RuntimeError(f"File not found after download")

    except Exception as e:
        print(f"Download error: {str(e)}")
        raise RuntimeError(f"Error during download: {str(e)}")


async def download_and_queue_song(ctx, player, url, skip_voice_check=False):
    try:
        if player.should_stop:
            return

        if not skip_voice_check:
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send("❌ Unable to join voice channel!")
                    return

        status_message = await ctx.send("⏳ Downloading...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, download_audio, url)

        if player.should_stop:
            if os.path.exists(result[0]):
                os.remove(result[0])
            await status_message.delete()
            return

        filepath, title = result
        player.add_to_queue(filepath, title)
        await status_message.edit(content=f"✅ **{title}** added to queue.")

        if not player.is_playing:
            await play_next(ctx)

    except Exception as e:
        await ctx.send(f"❌ Download error: {str(e)}")
        if "status_message" in locals():
            try:
                await status_message.delete()
            except:
                pass


async def play_next(ctx):
    try:
        player = music_players.get(ctx.guild.id)
        if not player or player.should_stop:
            return

        # Clean up previous file
        if player.now_playing and os.path.exists(player.now_playing["filepath"]):
            try:
                os.remove(player.now_playing["filepath"])
            except:
                pass
        player.now_playing = None

        next_track = player.next_in_queue()
        if next_track is None:
            player.is_playing = False
            if player.voice_client and player.voice_client.is_connected():
                await player.voice_client.disconnect()
            await ctx.send("🎵 Queue is empty!")
            return

        player.now_playing = next_track
        player.current_file = next_track["filepath"]

        # Check voice connection
        if not player.voice_client or not player.voice_client.is_connected():
            if not await join_voice_channel(ctx):
                await ctx.send("❌ Unable to join voice channel!")
                return

        title = next_track["title"]
        filepath = next_track["filepath"]

        # Check that file exists
        if not os.path.exists(filepath):
            await ctx.send(f"❌ File not found for {title}")
            await play_next(ctx)  # Try next song
            return

        player.is_playing = True

        def after_playing(error):
            if error:
                print(f"Playback error: {error}")
            player.is_playing = False
            # Schedule next song
            try:
                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            except Exception as e:
                print(f"Error scheduling next song: {e}")

        audio_source = discord.FFmpegPCMAudio(filepath)
        player.voice_client.play(audio_source, after=after_playing)
        await ctx.send(f"🎵 Now playing: **{title}**")

    except Exception as e:
        print(f"Error in play_next: {e}")
        player = music_players.get(ctx.guild.id)
        if player:
            player.is_playing = False
        await ctx.send(f"❌ Playback error: {str(e)}")


@bot.event
async def on_ready():
    print(f"{bot.user} is connected and ready!")
    print(f"Bot ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{COMMAND_PREFIX}help for command list",
        )
    )


@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes for cleanup"""
    if member == bot.user and before.channel and not after.channel:
        print(f"Bot was disconnected from voice channel: {before.channel.name}")

        guild = before.channel.guild
        if guild.id in music_players:
            player = music_players[guild.id]
            player.voice_client = None
            player.is_playing = False


@bot.command(name="play")
async def play(ctx, url: str):
    if not await join_voice_channel(ctx):
        return

    if ctx.guild.id not in music_players:
        music_players[ctx.guild.id] = MusicPlayer()

    player = music_players[ctx.guild.id]
    player.should_stop = False

    try:
        wait_msg = await ctx.send("⏳ Analyzing source...")

        # Try to extract as playlist
        loop = asyncio.get_event_loop()
        playlist_entries = await loop.run_in_executor(None, get_playlist_info, url)

        if playlist_entries:
            # It's a playlist
            await wait_msg.delete()

            # Make sure we're connected
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send("❌ Unable to join voice channel!")
                    return

            # Limit based on configuration
            playlist_entries = playlist_entries[:MAX_PLAYLIST_SIZE]
            await ctx.send(f"📋 Adding {len(playlist_entries)} tracks to queue...")

            for i, entry in enumerate(playlist_entries, 1):
                if player.should_stop:
                    await ctx.send("⏹️ Download stopped.")
                    return

                # Build video URL
                video_id = (
                    entry.get("id") or entry.get("url") or entry.get("webpage_url")
                )
                if not video_id:
                    continue

                if isinstance(video_id, str) and video_id.startswith("http"):
                    video_url = video_id
                else:
                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                title = entry.get("title", "Unknown title")
                await ctx.send(
                    f"⏳ [{i}/{len(playlist_entries)}] Adding **{title}**..."
                )

                await download_and_queue_song(
                    ctx, player, video_url, skip_voice_check=True
                )

            if not player.should_stop:
                await ctx.send("✅ All playlist tracks have been added!")
        else:
            # It's a single video
            await wait_msg.delete()
            if not player.should_stop:
                await download_and_queue_song(ctx, player, url)

    except Exception as e:
        await ctx.send(f"❌ An error occurred: {str(e)}")
        if "wait_msg" in locals():
            try:
                await wait_msg.delete()
            except:
                pass


@bot.command(name="stop")
async def stop(ctx):
    player = music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("❌ No music is currently playing.")
        return

    player.stop_and_clear()
    await player.disconnect_voice_client()
    await ctx.send("⏹️ Music stopped and queue cleared.")


@bot.command(name="reset")
async def reset_voice(ctx):
    """Force reset the voice connection"""
    await ctx.send("🔧 Resetting voice connection...")

    # Disconnect completely
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect(force=True)

    # Clean the player
    if ctx.guild.id in music_players:
        player = music_players[ctx.guild.id]
        await player.disconnect_voice_client()
        music_players[ctx.guild.id] = MusicPlayer()

    # Wait a bit
    await asyncio.sleep(2)
    await ctx.send("✅ Voice connection reset!")


@bot.command(name="list")
async def list_queue(ctx):
    player = music_players.get(ctx.guild.id)
    if not player or not player.queue:
        await ctx.send("❌ The queue is empty.")
        return

    queue_items = [f"{i+1}. {song['title']}" for i, song in enumerate(player.queue)]
    message = "🎶 **Queue:**\n" + "\n".join(queue_items[:10])

    if len(player.queue) > 10:
        message += f"\n... and {len(player.queue) - 10} more tracks"

    await ctx.send(message)


@bot.command(name="next")
async def next_track(ctx):
    player = music_players.get(ctx.guild.id)
    if not player or not player.is_playing:
        await ctx.send("❌ No music is currently playing.")
        return

    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.stop()
    else:
        await ctx.send("❌ No music is currently being played.")


@bot.event
async def on_disconnect():
    for file in os.listdir(temp_dir):
        try:
            os.remove(os.path.join(temp_dir, file))
        except:
            pass


def get_discord_token():
    """Get Discord token from environment variables or token file (fallback)"""
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        try:
            with open("token", "r") as f:
                TOKEN = f.read().strip()
            print(
                "⚠️  Warning: Using token file. Consider using DISCORD_TOKEN environment variable for better security."
            )
        except FileNotFoundError:
            print("❌ Error: No Discord token found!")
            print(
                "Please set DISCORD_TOKEN environment variable or create a 'token' file."
            )
            print("Get your token from: https://discord.com/developers/applications")
            exit(1)
    return TOKEN


if __name__ == "__main__":
    print("Starting Discord Music Bot (fixed version)")
    print("-" * 50)
    TOKEN = get_discord_token()
    bot.run(TOKEN)

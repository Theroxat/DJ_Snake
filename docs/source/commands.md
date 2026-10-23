# Commands

Every command works two ways: as a text message with the prefix, `!` by default (change it with `COMMAND_PREFIX`), and as a slash command (`/play`, `/next`, ...). The tables below show the prefix form. Slash commands need the bot invited with the `applications.commands` scope (see [Installation](installation.md)); they are published at startup, and Discord can take up to an hour to show them in every server (`SLASH_SYNC_GUILD` makes it instant on one server). `/crossfade` offers `on` and `off`, and `/theme` suggests the presets. Commands only work in a server, not in direct messages. On success the bot usually just adds a reaction to your message (a private ✅ for slash commands); slash command errors are only visible to you. Only errors, `!list`, `!reset`, usage help and the playlist prompt are text replies.

| Command | Description |
| --- | --- |
| `!play <YouTube or Spotify link, or search text>` | Join your voice channel and queue a YouTube video or playlist, a Spotify playlist, album or track, or the best YouTube match for the text. |
| `!list` | Show the current queue. |
| `!next` | Skip to the next track. |
| `!pause` / `!resume` | Pause or resume playback. |
| `!seek <pos>` | Jump to a position in the track, in seconds or `mm:ss`, as the clip page shows it. |
| `!crossfade on\|off` | Enable or disable crossfade for this session (off by default). |
| `!loop [off\|track\|queue]` | Repeat the current track or the whole queue; with no argument, cycle through the modes (off by default). |
| `!shuffle` | Shuffle the upcoming queue once; the current track keeps playing. |
| `!volume [0-150]` | Show the server's volume, or set it in percent (100 is the normal level). Saved across restarts. |
| `!autoplay [on\|off]` | Keep playing related tracks when the queue runs out, or show the current setting (off by default, see `AUTOPLAY`). |
| `!playlist save\|add\|load\|show\|delete <name>`, `!playlist list` | Saved playlists for the server: links and titles kept across restarts, downloaded again when loaded. |
| `!stop` | Stop, clear the queue, delete the downloaded files and leave voice. |
| `!reset` | Force a clean voice reconnect. |
| `!theme [preset\|reset\|edit]` | Show or set the clip page's default theme for the server. `edit` DMs you a private editor link. Needs the Manage Server permission. |
| `!weblink` | DM you the clip page's control link and its view-only link. DJs only when control is limited. |
| `!djrole [role\|off]` | Show, set or remove the DJ role. Needs the Manage Server permission. |

## Playing music

### `!play <link or text>`

You must be in a voice channel. The bot joins it, posts the clip page link and a status message, and downloads the track.

- **A YouTube video link** queues that track.
- **A YouTube playlist link** queues up to `MAX_PLAYLIST_SIZE` tracks. They download one at a time while the earlier ones play. A single message shows the progress and is edited at the end.
- **A radio or mix link** (a video inside a playlist) makes the bot ask, with buttons, whether to play the single track or the whole list. If nobody answers in time it plays the single track.
- **Plain text** (anything that is not a link, e.g. `!play daft punk one more time`, up to 200 characters) searches YouTube and queues the best match, using the same scoring as Spotify tracks. If nothing scores high enough the bot replies that no match was found.
- **A Spotify link** (`/playlist/`, `/album/` or `/track/`) is matched to YouTube videos. See [Spotify](spotify.md).

- **A SoundCloud or Bandcamp link** (track, set or album) is downloaded from the platform; **a Deezer or Apple Music link** is matched to YouTube like Spotify. See [More sources](sources.md).

The clip page's add box accepts only YouTube, SoundCloud, Bandcamp, Spotify, Deezer and Apple Music hosts. Without a link, the bot answers with a usage message.

### `!list`

Shows the queue. If it is empty the bot says so.

### `!next`

Skips the current track. Going back to the previous track is only available on the [clip page](clip-page.md).

### `!pause` and `!resume`

Pause and resume playback. The clip video pauses with it.

### `!seek <position>`

Jumps to a position in seconds (`90`) or as `mm:ss` (`1:30`). The position is relative to the start of the track as the clip page shows it: leading silence trimmed by automix is not counted. An invalid value gives an error message.

### `!crossfade on|off`

Turns crossfade on or off for the current session. When on, tracks fade out for 6 seconds and the next track fades in over the end of the previous one. It only overlaps when both tracks are long enough (at least 15 seconds of music each); otherwise tracks follow each other as usual. Off by default.

### `!loop [off|track|queue]`

Sets the loop mode for the session, or cycles off, track, queue when called without an argument. In `track` mode the current track replays each time it ends (skipping still moves on). In `queue` mode each finished or skipped track goes back to the end of the queue, so the list repeats. Each pass counts as one play in the listening stats. It works with crossfade. Off by default.

### `!shuffle`

Shuffles the upcoming queue once. The current track keeps playing and the order is not kept afterwards. It needs at least two queued tracks.

### `!volume [0-150]`

Without an argument, shows the server's volume. With a number from 0 to 150 (a trailing `%` is fine), sets it. 100 is the normal level (tracks are normalised to -14 LUFS); 0 is silent and 150 is about 3.5 dB louder, with a soft limiter so loud peaks are squeezed instead of clipping. The change applies to the playing track at once, without restarting it, and glides over a tenth of a second to avoid clicks. The volume belongs to the server, is shared with the clip page and is saved in `DATA_DIR/volume.json`.

### `!autoplay [on|off]`

Keeps the music going when the queue runs out. With autoplay on, the bot picks a track related to the last one (from YouTube's mix for that video), downloads it while the current track is still playing so there is no gap, and plays it. Tracks played in the last 50, live streams, anything over 10 minutes and titles that look like podcasts, interviews or compilations are skipped. If nothing suitable is found, the bot lingers in voice as usual. A loop mode, when active, takes precedence over autoplay, and a track you queue always plays before an autoplay pick. Without an argument, shows the current setting. Off by default; `AUTOPLAY=1` changes the default. The status message shows "📻 Autoplay on" and marks autoplay tracks.

### `!playlist`

Saved playlists belong to the server and survive restarts. Only the YouTube link, title and duration of each track are stored, never the media: loading a playlist downloads its tracks again. Names are case-insensitive, up to 32 characters (letters, digits, spaces and `. ' & ! + ( ) -`). A server holds up to 50 playlists of up to 200 tracks each.

- `!playlist save <name>` saves the current track and the queue. Saving over an existing name replaces it, but only its creator or a member with Manage Server can do that.
- `!playlist add <name>` appends the current track to an existing playlist.
- `!playlist load <name>` joins your voice channel and queues every track, loading them one at a time like a YouTube playlist.
- `!playlist list` shows the server's playlists, and `!playlist show <name>` the tracks of one.
- `!playlist delete <name>` deletes a playlist. Only its creator or a member with Manage Server can.

The [clip page](clip-page.md) can also save the queue and load a playlist.

### `!stop`

Stops playback, empties the queue, deletes downloaded files and leaves the voice channel at once. The clip page link stays valid until the session expires.

### `!reset`

Forces a clean voice reconnect. Use it when the bot is connected but silent, or after a Discord voice error such as close code 4006.

## Server settings

### `!theme [preset|reset|edit]`

Needs the **Manage Server** permission.

| Form | Effect |
| --- | --- |
| `!theme` | Shows the current theme, whether it is the server's choice or the fallback, and the available presets. |
| `!theme <preset>` | Sets the server's default theme: `arcade`, `minimal`, `night`, `vinyl`, `club`, `terminal`, `space`, `cobalt` or `custom`. |
| `!theme reset` | Goes back to the `WEB_THEME` default and drops the custom colors, name and logo. |
| `!theme edit` | Sends you a private link to the theme editor by direct message. It is valid for 30 minutes. |

See [Themes](clip-page.md#themes) for details. `!theme edit` needs your DMs open to server members; otherwise the bot tells you so.

### `!djrole [role|off]`

Needs the **Manage Server** permission. The setting is saved per server in `DATA_DIR/permissions.json`.

| Form | Effect |
| --- | --- |
| `!djrole` | Shows the current DJ role, or that everyone controls the music. |
| `!djrole <role>` | Makes a role (name, ID or mention) the DJ role. |
| `!djrole off` | Removes it: everyone controls the music again. |

With a DJ role set:

- `!next`, `!pause`, `!resume`, `!seek`, `!crossfade`, `!stop` and `!reset` need the DJ role or the Manage Server permission. The member who queued the playing track may still skip it with `!next`.
- `!play` and `!list` stay open to everyone: adding a track never interrupts what plays, and DJs can still skip or remove it.
- The status message shows only the clip page's view-only link, and the control link posted before stops working. See [Who controls the music](clip-page.md#who-controls-the-music).

Without a DJ role every command works for everyone, as in earlier versions.

### `!weblink`

Sends you, by direct message, the clip page's control link and its view-only link, opening a session (without joining voice) if the server has none. When control is limited (a DJ role is set, or `WEB_CONTROL=dj`), only DJs and members with Manage Server get it; with `WEB_CONTROL=dj` and no DJ role, only Manage Server. Your DMs must be open to server members.

## Errors

If you leave out a required argument the bot says so. Commands that need music playing answer "No music is currently playing." when there is none, and `!theme` without the permission is refused. A command reserved for DJs answers, for example, "Only DJs can use `!stop`: you need the **DJ** role or the **Manage Server** permission."

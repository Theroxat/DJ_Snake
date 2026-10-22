# Commands

Commands are text messages sent in a server channel. The prefix is `!` by default (change it with `COMMAND_PREFIX`). Commands only work in a server, not in direct messages. On success the bot usually just adds a reaction to your message; only errors, `!list`, `!reset`, usage help and the playlist prompt are text replies.

| Command | Description |
| --- | --- |
| `!play <YouTube or Spotify link>` | Join your voice channel and queue a YouTube video or playlist, or a Spotify playlist, album or track. |
| `!list` | Show the current queue. |
| `!next` | Skip to the next track. |
| `!pause` / `!resume` | Pause or resume playback. |
| `!seek <pos>` | Jump to a position in the track, in seconds or `mm:ss`, as the clip page shows it. |
| `!crossfade on\|off` | Enable or disable crossfade for this session (off by default). |
| `!stop` | Stop, clear the queue, delete the downloaded files and leave voice. |
| `!reset` | Force a clean voice reconnect. |
| `!theme [preset\|reset\|edit]` | Show or set the clip page's default theme for the server. `edit` DMs you a private editor link. Needs the Manage Server permission. |

## Playing music

### `!play <link>`

You must be in a voice channel. The bot joins it, posts the clip page link and a status message, and downloads the track.

- **A YouTube video link** queues that track.
- **A YouTube playlist link** queues up to `MAX_PLAYLIST_SIZE` tracks. They download one at a time while the earlier ones play. A single message shows the progress and is edited at the end.
- **A radio or mix link** (a video inside a playlist) makes the bot ask, with buttons, whether to play the single track or the whole list. If nobody answers in time it plays the single track.
- **A Spotify link** (`/playlist/`, `/album/` or `/track/`) is matched to YouTube videos. See [Spotify](spotify.md).

Only YouTube and Spotify hosts are accepted. Without a link, the bot answers with a usage message.

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
| `!theme <preset>` | Sets the server's default theme: `arcade`, `minimal`, `night`, `vinyl`, `club`, `terminal`, `space` or `custom`. |
| `!theme reset` | Goes back to the `WEB_THEME` default and drops the custom colors, name and logo. |
| `!theme edit` | Sends you a private link to the theme editor by direct message. It is valid for 30 minutes. |

See [Themes](clip-page.md#themes) for details. `!theme edit` needs your DMs open to server members; otherwise the bot tells you so.

## Errors

If you leave out a required argument the bot says so. Commands that need music playing answer "No music is currently playing." when there is none, and `!theme` without the permission is refused.

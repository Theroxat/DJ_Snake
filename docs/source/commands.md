# Commands

Every command works two ways: as a text message with the prefix, `!` by default (change it with `COMMAND_PREFIX`), and as a slash command (`/play`, `/next`, `/playlist save`, ...). The tables below show the prefix form. `/play` takes one `query` option: a link or search text. Slash commands need the bot invited with the `applications.commands` scope (see [Installation](installation.md)); they are published at startup, and Discord can take up to an hour to show them in every server (`SLASH_SYNC_GUILD` makes it instant on one server). `/crossfade` offers `on` and `off`, and `/theme` suggests the presets. Commands only work in a server, not in direct messages. On success the bot usually just adds a reaction to your message (a private ✅ for slash commands); slash command errors are only visible to you. Only errors, `!list`, `!reset`, usage help and the playlist prompt are text replies.

| Command | Description |
| --- | --- |
| `!play <link or search text>` | Join your voice channel and queue a YouTube video or playlist, a SoundCloud or Bandcamp track or set, a Spotify, Deezer or Apple Music playlist, album or track, or the best YouTube match for the text. |
| `!list` | Show the current queue. |
| `!remove <position>` | Remove a queued track by its position in `!list` (1 = next). Autocompletes the queue. Anyone may remove a track they queued themselves. |
| `!move <from> <to>` | Move a queued track to another position. Autocompletes the queue. |
| `!previous` | Go back to the previous track, or restart the current one after 5 s of play. |
| `!clear` | Empty the upcoming queue; the current track keeps playing. |
| `!nowplaying` (`!np`) | Show the playing track as an embed: requester, source, progress bar with elapsed/total, loop, autoplay and volume, and the clip page link. |
| `!history [count]` | The last tracks played in this session (default 10, max 25), newest first. |
| `!next` | Skip to the next track. |
| `!voteskip` (`!vs`), `!voteskip threshold <1-100>` | Vote to skip the current track, open to everyone in the bot's voice channel (more than 50% of the listeners by default). DJs and the requester skip at once. The threshold is set by DJs, see `VOTESKIP_PERCENT`. |
| `!pause` / `!resume` | Pause or resume playback. |
| `!seek <pos>` | Jump to a position in the track, in seconds or `mm:ss`, as the clip page shows it. |
| `!crossfade on\|off` | Enable or disable crossfade for this session (off by default). |
| `!loop [off\|track\|queue]` | Repeat the current track or the whole queue; with no argument, cycle through the modes (off by default). |
| `!shuffle` | Shuffle the upcoming queue once; the current track keeps playing. |
| `!volume [0-150]` | Show the server's volume, or set it in percent (100 is the normal level). Saved across restarts. |
| `!autoplay [on\|off]` | Keep playing related tracks when the queue runs out, or show the current setting (off by default, see `AUTOPLAY`). |
| `!limits [queue\|user\|minutes\|cooldown] [number\|default]` | Show the queue limits, or change one (Manage Server): queue size, tracks per member, track length, /play cooldown. |
| `!playlist save\|add\|load\|show\|delete <name>`, `!playlist list` | Saved playlists for the server: links and titles kept across restarts, downloaded again when loaded. |
| `!stop` | Stop, clear the queue, delete the downloaded files and leave voice. |
| `!reset` | Force a clean voice reconnect. |
| `!theme [preset\|reset\|edit]` | Show or set the clip page's default theme for the server. `edit` DMs you a private editor link. Needs the Manage Server permission. |
| `!weblink` | DM you the clip page's control link and its view-only link. DJs only when control is limited. |
| `!djrole [role\|off]` | Show, set or remove the DJ role. Needs the Manage Server permission. |
| `!language [code\|reset]` | Show or set the language of the bot and the clip page for the server (`en`, `fr`). Needs the Manage Server permission. |
| `!help [command]` | List the commands, or show one, in the server's language. |

## Playing music

### `!play <link or text>`

You must be in a voice channel. The bot joins it, posts the clip page link and a status message, and downloads the track.

- **A YouTube video link** queues that track.
- **A YouTube playlist link** queues up to `MAX_PLAYLIST_SIZE` tracks. They download one at a time while the earlier ones play. A single message shows the progress and is edited at the end.
- **A radio or mix link** (a video inside a playlist) makes the bot ask, with buttons, whether to play the single track or the whole list. If nobody answers in time it plays the single track.
- **Plain text** (anything that is not a link, e.g. `!play daft punk one more time`, up to 200 characters) searches YouTube and queues the best match, using the same scoring as Spotify tracks. If nothing scores high enough the bot replies that no match was found.
- **A Spotify link** (`/playlist/`, `/album/` or `/track/`) is matched to YouTube videos. See [Spotify](spotify.md).
- **A SoundCloud or Bandcamp link** (track, set or album) is downloaded from the platform; **a Deezer or Apple Music link** is matched to YouTube like Spotify. See [More sources](sources.md).

`!play` and the clip page's add box accept only YouTube, SoundCloud, Bandcamp, Spotify, Deezer and Apple Music hosts. Links are rebuilt in canonical form before use (a YouTube video inside a playlist keeps both ids, for the one-track-or-list prompt); a link from any other host is refused with a "link not accepted" message. Without an argument, the bot answers with a usage message.

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

### `!remove <position>`, `!move <from> <to>`, `!previous`, `!clear`

Queue management that used to exist only on the clip page. Positions are the 1-based numbers `!list` shows (1 is the next track); a position outside the queue gives an "Invalid position" error. As slash commands, `/remove` and `/move` suggest the queued tracks (position and title). `!previous` restarts the current track after 5 s of play (or with no history) and otherwise goes back to the last played one. `!clear` empties the upcoming queue and keeps the current track playing.

### `!nowplaying` (alias `!np`)

An embed with the track title (linked), who requested it, the source (YouTube, SoundCloud, Bandcamp), the thumbnail, a progress bar with elapsed and total time (as the clip page shows them), the loop, autoplay and volume state, and the clip page link. The link follows the usual rules: the control link when control is open to everyone, otherwise the view-only one.

### `!history [count]`

The last tracks played in the session, newest first, with duration and requester. `count` defaults to 10 and is capped at 25; the list is cut to fit Discord's message limit. It is cleared by `!stop`.

### `!volume [0-150]`

Without an argument, shows the server's volume. With a number from 0 to 150 (a trailing `%` is fine), sets it. 100 is the normal level (tracks are normalised to -14 LUFS); 0 is silent and 150 is about 3.5 dB louder, with a soft limiter so loud peaks are squeezed instead of clipping. The change applies to the playing track at once, without restarting it, and glides over a tenth of a second to avoid clicks. The volume belongs to the server, is shared with the clip page and is saved in `DATA_DIR/volume.json`.

### `!voteskip` and `!voteskip threshold <1-100>`

For servers with a DJ role: members who cannot use `!next` can ask to skip the track instead (the refusal message suggests it). `!voteskip` (alias `!vs`, or `/voteskip`) records your vote for the current track; you must be in the bot's voice channel. Once more than the threshold share of the listeners have voted (50% by default: 3 of 5, 2 of 3, everyone when there are 2), the track is skipped. DJs and the member who queued the track skip at once, as with `!next`. Who counts: the humans in the channel who are not deafened (a deafened member does not hear the track, so does not count in the total and cannot vote). Votes are counted again whenever someone joins, leaves or deafens, so members who left no longer count, and the votes start over with each new track. The bot posts one message with the count ("🗳️ Vote to skip: 2/3 votes"), edits it as votes change and finishes it with "Skipped by vote". The status message shows the count while votes are active, and the clip page shows it in the header (it cannot vote: it has no identity). `!voteskip threshold <1-100>` sets the server's share (DJ role or Manage Server; without an argument it shows it) and is saved in `DATA_DIR/voteskip.json`; `VOTESKIP_PERCENT` sets the default.

### `!autoplay [on|off]`

Keeps the music going when the queue runs out. With autoplay on, the bot picks a track related to the last one (from YouTube's mix for that video), downloads it while the current track is still playing so there is no gap, and plays it. Tracks played in the last 50, live streams, anything over 10 minutes and titles that look like podcasts, interviews or compilations are skipped. If nothing suitable is found or no pick is ready in time, the bot lingers in voice as usual, and it never autoplays into an empty channel. A loop mode, when active, takes precedence over autoplay, and a track you queue always plays before an autoplay pick. Without an argument, shows the current setting. Off by default; `AUTOPLAY=1` changes the default. The status message shows "📻 Autoplay on" and marks autoplay tracks.

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

### Queue limits

`!limits` (`/limits`) shows the server's limits in a small embed. `!limits <setting> <number>` changes one and `!limits <setting> default` goes back to the value from `.env`. Changing needs the **Manage Server** permission; showing is open to everyone. The settings are saved per server in `DATA_DIR/limits.json`.

| Setting | `.env` default | Meaning |
| --- | --- | --- |
| `queue` | `MAX_QUEUE_SIZE` (200) | Tracks waiting in the queue (at least 1). |
| `user` | `MAX_TRACKS_PER_USER` (0) | Tracks one member may have waiting; 0 is unlimited. |
| `minutes` | `MAX_TRACK_MINUTES` (0) | Longest single track; 0 is unlimited. |
| `cooldown` | `PLAY_COOLDOWN_SECONDS` (0) | Seconds between one member's `/play` calls; 0 is off. |

The limits apply wherever tracks enter the queue: `/play` (links, playlists, Spotify, Deezer, Apple Music and text search), the clip page's add, `/playlist load` and the page's playlist load. Autoplay only obeys the queue size. The clip page has no identity, so everything added from it counts as one member (`web`) for the per-member limit; it is checked against the queue size and the track length like anyone else, and it has no cooldown.

DJs (the DJ role) and members with Manage Server skip the per-member limit and the cooldown, never the queue size or the track length. Without a DJ role only Manage Server members skip them.

The track length is read from the link's metadata before downloading when the source gives it (playlist entries, Spotify, Deezer and Apple Music tracks), otherwise after the download, and the file is then deleted. A playlist queues as many tracks as the limits allow, and one message at the end says how many were skipped and why, for example "12/30 tracks loaded (18 skipped: queue limit (12))". The bot only downloads whole files, so there are no live streams to exempt.

### `!djrole [role|off]`

Needs the **Manage Server** permission. The setting is saved per server in `DATA_DIR/permissions.json`.

| Form | Effect |
| --- | --- |
| `!djrole` | Shows the current DJ role, or that everyone controls the music. |
| `!djrole <role>` | Makes a role (name, ID or mention) the DJ role. |
| `!djrole off` | Removes it: everyone controls the music again. |

With a DJ role set:

- `!next`, `!pause`, `!resume`, `!seek`, `!crossfade`, `!loop`, `!shuffle`, `!previous`, `!move`, `!clear`, `!volume`, `!autoplay`, `!stop` and `!reset` need the DJ role or the Manage Server permission. The member who queued the playing track may still skip it with `!next`, `!remove` is open to whoever queued the track being removed, and everyone can use `!voteskip`. `!play`, `!list`, `!nowplaying` and `!history` stay open to everyone.
- `!play` and `!list` stay open to everyone: adding a track never interrupts what plays, and DJs can still skip or remove it.
- The status message shows only the clip page's view-only link, and the control link posted before stops working (every change of the role gives a live session a new one and retires the server's `!theme edit` links). See [Who controls the music](clip-page.md#who-controls-the-music).

Without a DJ role every command works for everyone, as in earlier versions.

### `!weblink`

Sends you, by direct message, the clip page's control link and its view-only link, opening a session (without joining voice) if the server has none. When control is limited (a DJ role is set, or `WEB_CONTROL=dj`), only DJs and members with Manage Server get it; with `WEB_CONTROL=dj` and no DJ role, only Manage Server. Your DMs must be open to server members.

### `!language [code|reset]`

Needs the **Manage Server** permission. Aliases: `!lang`, `!langue`.

| Form | Effect |
| --- | --- |
| `!language` | Shows the current language, whether it is the server's choice or the fallback, and the available languages. |
| `!language <code>` | Sets the server's language: `en` (English) or `fr` (French). |
| `!language reset` | Goes back to the `BOT_LANGUAGE` default. |

The language applies to command replies, errors, `!help`, the status message and the clip page's default language (each viewer can still pick another one in the page's settings menu). Command names and their arguments stay the same in every language.

## Errors

If you leave out a required argument the bot says so. Commands that need music playing answer "No music is currently playing." when there is none, and `!theme`, `!djrole`, `!language` or a `!limits` change without the permission is refused. A command reserved for DJs answers, for example, "Only DJs can use `!stop`: you need the **DJ** role or the **Manage Server** permission." Error messages follow the server's language.

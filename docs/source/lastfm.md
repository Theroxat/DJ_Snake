# Last.fm scrobbling

DJ Snake can send what you listen to in the voice channel to your [Last.fm](https://www.last.fm) profile. It is optional and off by default, and every user links their own account.

## Set it up (server owner)

1. Create an API account at [last.fm/api/account/create](https://www.last.fm/api/account/create). Any name works.
2. Put the API key and the shared secret in `.env`:

   ```bash
   LASTFM_API_KEY=your_api_key
   LASTFM_API_SECRET=your_shared_secret
   ```

3. Restart the bot. Without both values the `/lastfm` commands answer that the feature is not enabled.

The users' session keys are stored in `DATA_DIR/lastfm.json`, readable by the bot's user only (mode 0600), and are never logged or shown.

## Link your account (each user)

1. Run `/lastfm link`. The bot answers only to you (a private message for `!lastfm link`) with a Last.fm link.
2. Open it, sign in to Last.fm and allow DJ Snake.
3. Press **Done** under the message (or run `/lastfm link` again). The bot confirms the Last.fm user name.

`/lastfm status` shows the linked account, and `/lastfm unlink` removes it with any scrobble still waiting. The link is yours across all servers.

## What gets scrobbled

- Every linked user who is in the bot's voice channel when a track starts gets "now playing", and the track is scrobbled for them when it ends if they were still in the channel. Someone who joins halfway or leaves early gets nothing for that track.
- Last.fm's rules apply: the track must last at least 30 seconds and be played for half its length or 4 minutes, whichever comes first. Skipping early scrobbles nothing. Autoplay tracks are scrobbled like any other.
- Artist, title, album and duration come from the Spotify, Deezer or Apple Music link when the track was queued from one. Otherwise the artist and title are read from the YouTube title ("Artist - Title", with "(Official Video)" and "ft. ..." removed, or the channel name when there is no dash). Tracks with no readable artist or title are skipped.

## When Last.fm is down

Scrobbles never delay or interrupt playback. Failed ones are kept (up to 500 per user, in memory) and retried in batches of 50, waiting 30 seconds and doubling up to 10 minutes. They are lost if the bot restarts before Last.fm answers, apart from one last attempt at shutdown. If Last.fm says a session key is no longer valid, the account is unlinked and the user needs to link again.

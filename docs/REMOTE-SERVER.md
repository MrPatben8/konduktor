# Remote libraries: a Konduktor server on your NAS

Keep your whole library — the collection file and the music — on a server, and
open it from any computer with Konduktor. Prep tracks, edit playlists, and
export sticks for your gigs without the music ever living on your performance
laptop.

The server is a small container (`server/`). It holds the library and saves it.
Playback, analysis and stem separation run on the computer you open it from,
on copies it downloads, so the server needs no GPU and does very little work.

**On the server (e.g. TrueNAS SCALE):**

1. Copy `server/compose.yaml` and `server/.env.example` (renamed to `.env`)
   onto the NAS. Point the volumes at your datasets, and fill in `.env`: the
   platform (`traktor` or `rekordbox`), the library file, the music folder, a
   username and password, and — for a collection made on a Mac — the path
   mapping (`/Users/you/Music => /music`).
2. Install it: on TrueNAS, **Apps → Discover Apps → ⋮ → Install via YAML** and
   paste the compose file; anywhere else, `docker compose up -d`. A bad setting
   stops the container with one clear line in its log.

**In the app:** choose **Remote** on the platform screen → **Add a remote
library** → enter the server's address (and a fallback, such as its Tailscale
address, for when you're away from home), the username and the password.
Konduktor checks the server before it saves the remote, and keeps the
password in your keychain.

Good to know:

- **One computer at a time.** If another computer has the library open,
  Konduktor says who has it and offers to take over. Edits that computer
  hadn't saved stay with the library, and you're asked to save or discard
  them.
- **Plain HTTP.** Reach the server over your home network or a VPN
  (Tailscale / WireGuard). Don't expose its port to the internet.
- **Unsaved edits live on the server** until you save. Restarting the
  container drops them.
- Downloaded tracks are cached on your computer (Settings → **Remote audio**
  sets the size), so a track you've prepped and then export is fetched once.

# Shisu-ko phone portal

A phone-sized web page, served by the computer that runs the Shisu-ko server: a YouTube video with
Shisu-ko's subtitles, word lookups through Yomitan, and one-tap mining into Anki.

Start it with `server/portal.cmd` (Windows), `server/portal.sh` (Linux, macOS) or
`nix run .#portal`; see "Phone portal" in the main [README](../README.md) for the setup, and
[docs/dev/portal.md](../docs/dev/portal.md) for how it works and its security model.

Options (`portal.py --help`):

| Option | Default | |
|---|---|---|
| `--mode local\|lan\|tailscale` | `$PORTAL_MODE`, else `local` | who may use it: this computer; paired devices on the network; your own Tailscale devices |
| `--host ADDR` | per mode | address to listen on (repeatable; `$PORTAL_HOST`, comma-separated) |
| `--port N` | `$PORTAL_PORT`, else 8791 | |
| `--host-name NAME` | | another name the page may be reached by (repeatable) |
| `--forget-devices` | | revoke every paired device, then exit |
| `--config PATH` | `~/.shisu-ko/portal.json` | card settings that override the page's (optional) |
| `--server`, `--yomitan`, `--anki` | `127.0.0.1:8790`, `:19633`, `:8765` | where the services are |

A `portal.json` that fixes the card setup, for example:

```json
{
  "deck": "Japanese::Mining",
  "note_model": "Basic",
  "fields": { "Front": "{expression}", "Back": "{glossary}" }
}
```

The Shisu-ko server, not the portal, syncs Anki with AnkiWeb after mining; Settings > Anki shows
how its last sync went. An `"auto_sync"` key left from 0.20.0 or 0.21.0 is ignored, with a warning
at start.

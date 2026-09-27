Shisu-ko shows live Japanese subtitles on YouTube. It is half of a two-part program: a companion server (server/server.py in the same repository, Python, MIT) runs on the user's own computer at http://127.0.0.1:8790, downloads the audio of the video being watched with yt-dlp and transcribes it with Whisper. The extension sends it the video id and the playback position, draws the cues it gets back as plain DOM text (textContent) so popup dictionaries can scan them, and on request sends a screenshot and an audio clip of a line to Anki through AnkiConnect (http://127.0.0.1:8765) or to Downloads. Optional word colours, off by default, read one Anki deck through AnkiConnect.

No remote code, no minified or generated code, no third-party libraries: the zip is the addon/ folder of https://github.com/Multysquid/shisu-ko at tag v<version>, without its tests; its manifest's version reads <version>.1, as the tag's own number went to the release's self-distributed build. Nothing is sent to us or to any third party; the one remote request is an anonymous GET of https://api.github.com/repos/Multysquid/shisu-ko/releases/latest, the update check, at most once a day.

Full guide (every feature's test steps, every permission and request): https://github.com/Multysquid/shisu-ko/blob/v<version>/docs/amo/reviewer-guide.md

QUICK TEST (10 minutes, no GPU, no account)
1. git clone https://github.com/Multysquid/shisu-ko && cd shisu-ko && bash server/setup.sh (answer 2, the small model, and N if asked about cookies), then server/run.sh --device cpu. Ready at "Listening on http://127.0.0.1:8790".
2. about:debugging > Load Temporary Add-on > the zip. The popup says "Server online"; click "Allow on YouTube" (optional host permission).
3. A YouTube video with Japanese speech (e.g. https://www.youtube.com/@cijapanese): subtitles appear after 10-30 s on CPU. Hovering one pauses the video; Alt+Shift+L opens the transcript. In the popup set mining to Downloads and press Alt+Shift+M: a JPEG and an MP3 land in Downloads/shisu-ko-mining/.

PERMISSIONS
storage: settings and small session records. downloads: the Downloads fallback for mined files (blob: URLs). tabs: reload YouTube tabs after the host grant; route keyboard commands. Hosts: youtube.com for the content script; 127.0.0.1 and localhost for the companion server and AnkiConnect. notifications: a newer release, from the check at browser start (once per release and session), and the outcome of an update the user asked for. nativeMessaging (optional, requested on the popup's "Start server" click): one message, {cmd: "start"}, to the host "shisuko" (server/native_host.py), which can only start the server's own launcher.

The content script builds all DOM with createElement/textContent (youtube.com enforces Trusted Types); the only markup inside a subtitle is <span class="shisuko-word"> with data-status/data-pitch from a fixed list. It reads YouTube's player through wrappedJSObject (numbers only) for a live stream's clock.

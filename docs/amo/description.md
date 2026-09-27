Shisu-ko shows live Japanese subtitles on YouTube in Firefox. A small server on your own computer transcribes the video's audio with OpenAI's Whisper a little ahead of where you are watching, and the extension draws the result over the player as ordinary page text. Nothing about you is sent to anyone, except, if you allow it, your browser's YouTube cookies to YouTube (and its GitHub cookies to GitHub, with the server option --allow-remote-ejs): the server runs on your machine, and the only network traffic is the video's audio from YouTube, the one-time model download and a look at GitHub for a newer release, by the server's launcher before each start and by the extension once a day (see Privacy below).

**What you get**

- **Subtitles a few seconds after a video opens.** The server keeps transcribing ahead of the playhead and caches every line, so seeking back or rewatching is instant. Live streams work too.
- **Text a dictionary can read.** Subtitles are real page text, so [Yomitan](https://yomitan.wiki/) or any popup dictionary scans them. Hovering a line pauses the video, the dictionary popup keeps it paused, and moving back over the video resumes it.
- **A transcript panel** with every line so far. A timestamp jumps there, a pickaxe mines it.
- **Sentence mining without pressing anything.** The moment Yomitan adds a card, Shisu-ko attaches a screenshot of the frame you were reading and an MP3 clip of the line you were reading, through AnkiConnect. The pickaxe on a line, or Alt+Shift+M, does the same on demand, into the newest card or into your Downloads folder.
- **Word colours, if you want them.** With Anki running, every word of a line that has a card in your deck is coloured by the card's state (green learned, yellow learning, orange suspended, red new), and can carry an overbar in the colour of its pitch accent, read from the card's pitch accent field. Switches in the popup, off by default, colour particles and katakana words green and names and Latin text blue, and a list of your own known words shows green (Alt+Shift+K adds the word under the pointer). The deck follows your mining, a verb is found in its conjugations, and the text stays scannable. Both colourings are off by default.
- **Your hardware, your model.** Whisper large-v3 or small, chosen and downloaded at setup, on an NVIDIA GPU, on an Apple Silicon GPU through MLX, or on the CPU; a Mac is offered large-v3-turbo instead of large-v3, which does not fit in the GPU memory beside the browser. The popup switches to the Japanese-specialised kotoba-whisper (about 6x faster) or to any other faster-whisper model, without restarting the server.
- **Your fonts.** The subtitle font is a preset (gothic, rounded, mincho) or any font installed on your computer, with position, colour, box and outline adjustable.
- **A Start button for the server.** When the server is not running, the popup starts it for you; Firefox asks once for permission to talk to the small launcher that the server's setup registers.
- **Updates without leaving the browser.** The popup tells you when a newer release is out, and one click makes the server update itself and restart; the extension itself is updated by Firefox from this listing.

**You need the companion server**

The extension does nothing on its own. Download the server from the project page, start it, and the extension finds it at http://127.0.0.1:8790:

- Windows: run `server\setup.cmd` once, then `server\run.cmd`.
- Linux and macOS: `bash server/setup.sh` once, then `server/run.sh`.
- Also available as a Nix flake and as a Docker image with GPU support.

Requirements: Python 3.10 or newer, Node.js 20+ or Deno (yt-dlp needs a JavaScript runtime for YouTube), and a GPU for the large models: an NVIDIA card with about 4 GB of free VRAM, or any Apple Silicon Mac, where the server decodes on the Mac's own GPU with large-v3-turbo. Without either, pick the small model at setup and run on the CPU. Optional: Yomitan for lookups, Anki with the AnkiConnect add-on for mining and the word colours.

**Shortcuts**

- Alt+Shift+S turns Shisu-ko on or off (the switch in the popup header).
- Alt+Shift+L toggles the transcript panel.
- Alt+Shift+M mines the current sentence.
- Alt+Shift+K marks the word under the pointer as known, or takes it off your known words again.
- Alt+Shift+H hides the status badge on the video, or shows it again.
- Left and Right jump to the previous or next subtitle (can be turned off).

**Privacy**

The extension talks to the server on your own computer and, if you use mining or the word colours, to Anki on your own computer. Its one remote request is an anonymous look at GitHub for the newest release, once a day (a check that failed, offline for instance, is tried again the next time the popup opens) and when you click Check for updates. It has no account, no analytics and no remote code. The server sends YouTube the cookies of your YouTube sign-in with its downloads only if you allow it (setup asks, for when YouTube refuses downloads without one); started with --allow-remote-ejs as well, it also sends GitHub that browser's GitHub cookies when yt-dlp fetches its challenge-solver script there. The privacy policy on this page lists exactly what is exchanged with those programs.

Shisu-ko is free software under the MIT license. Source code, setup guide, server options and troubleshooting: https://github.com/Multysquid/shisu-ko

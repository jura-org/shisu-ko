# Shisu-ko

[![Tests](https://github.com/Multysquid/shisu-ko/actions/workflows/tests.yml/badge.svg)](https://github.com/Multysquid/shisu-ko/actions/workflows/tests.yml)

Live Japanese subtitles for YouTube in Firefox and Chrome: transcribed locally,
readable with [Yomitan](https://yomitan.wiki/).

![Shisu-ko on a YouTube video: hovering a subtitle pauses the video, Yomitan looks up シルバーウィーク, adding the card makes Shisu-ko attach the frame and the sentence audio, and Anki shows the finished card](docs/demo.gif)

[with sound](docs/demo.mp4).

Shisu-ko works as a 2 pair application. For one there is a server that transcribes audio via an ASR model of the users choosing, additionally there is a the Firefox or Chrome addon that allows the server to send the transcriptions to the local browser and display them. 

**[Install](#install)** · [Using it](#using-shisu-ko) · [Sentence mining](#sentence-mining) ·
[Word colours](#word-colours) · [Phone portal](#phone-portal) · [Server options](docs/server.md) ·
[Troubleshooting](#troubleshooting) · [Privacy](#privacy)

## Requirements

Setup asks which model to download. Pick the one that fits your computer:

| Your computer | Model | Download | Notes |
|---|---|---|---|
| NVIDIA graphics card with about 4 GB of free memory | `large-v3` | about 3 GB | Best quality |
| Mac with Apple Silicon | `large-v3-turbo` | about 1.6 GB | Runs on the Mac's own GPU |
| No suitable graphics card | `small` | about 500 MB | Runs on the CPU, less accurate |
| AMD Radeon RX 7000 or RX 9000, and some others | `large-v3` | about 3 GB | Experimental, see [AMD graphics cards](docs/amd.md) |

Alternatvely the Server also supports:

[Kitsune-Transcribe](docs/server.md#kitsune-transcribe-models) models: small, Japanese-only
models that run on an NVIDIA graphics card or on the CPU.

You also need:

- Firefox 140 or newer, or Chrome 120 or newer.
- Windows, Linux or macOS with Python 3.10 or newer, and Node.js 20+ or Deno (yt-dlp needs one of
  them for YouTube). Nix (on x86-64 Linux) and Docker work too.
- Optional: [Yomitan](https://yomitan.wiki/) to look words up, and [Anki](https://apps.ankiweb.net/)
  with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on for mining.


## Install

> [!NOTE]
> Only want subtitles? Steps 1 to 3 are all you need. Yomitan and Anki are optional.

### 1. Start the server

First install [Python](https://www.python.org/downloads/) 3.10 or newer, and
[Deno](https://deno.com/) or [Node.js](https://nodejs.org/) 20 or newer. On Windows, tick
**Add python.exe to PATH** in Python's installer.

Then get Shisu-ko, into a folder you will keep. Either download **Source code (zip)** from the
[latest release](https://github.com/Multysquid/shisu-ko/releases/latest) and extract it (on
Windows: right-click, **Extract All...**), or clone it with git. The `-firefox.zip` and
`-chrome.zip` files on the release page hold only the extension.

```
git clone https://github.com/Multysquid/shisu-ko
cd shisu-ko
```

Run setup once, then start the server. On Windows, double-click `server\setup.cmd`, then
`server\run.cmd`. On Linux and macOS, run these in a terminal in the Shisu-ko folder:

```
bash server/setup.sh
bash server/run.sh
```

Setup installs what the server needs into the `.shisu-ko` folder in your home folder
(`C:\Users\<you>\.shisu-ko` on Windows). It asks two questions:

1. **Which model should the server use?** Type the corrosponding number of the model you want. As a default Whisper is good. 
2. **Should the server send Firefox's YouTube cookies with every download?** Type `Y` if you are
   signed in to YouTube in Firefox

The server is ready when it prints `Listening on http://127.0.0.1:8790`. Keep its window open
while you watch.

### 2. Install the extension

- **Firefox:** [Shisu-ko on addons.mozilla.org](https://addons.mozilla.org/firefox/addon/shisu-ko/).
- **Chrome:** [Shisu-ko in the Chrome Web Store](https://chromewebstore.google.com/detail/shisu-ko/ecenifonpkaiccmmknpbllbebbfigjnm).

Both update by themselves. Additionally you can start and stop the Server from the addon.

> [!IMPORTANT]
> In Firefox, open the Shisu-ko popup once. If it shows **Allow on YouTube**, click it: Firefox
> shows no subtitles until Shisu-ko may access youtube.com.


### 3. Mine into Anki (optional)

Install [Anki](https://apps.ankiweb.net/) with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159)
add-on, and keep Anki open while you watch. When you add a card with Yomitan, the server fills in
the line's audio and a picture of the video. See [Sentence mining](#sentence-mining).

### Running the server

- **The Start server button.** Setup registers a small launcher with your browsers. While the
  server is off, the popup shows a **Start server** button.
  **Start server** button always starts the server without options. See
  [the server options](docs/server.md#server-options).
- **Stopping.** Close the server's window, or press Ctrl+C in it. On Linux and macOS, a server the
  button started has no window. 

## Using Shisu-ko

### Reading with Yomitan

Hover a subtitle and the video pauses. Look words up with Yomitan as usual. If you want to mine the word do it as usual. A screenshot as well as audio will get automatically attached to the new card.

### Shortcuts

| Key | What it does |
|---|---|
| Alt+Shift+S | Turn Shisu-ko on or off |
| Alt+Shift+L | Show or hide the transcript panel |
| Alt+Shift+M | Mine the current line into your newest Anki card |
| Alt+Shift+K | Mark the word under the pointer as known, or unmark it |
| Alt+Shift+H | Hide or show the status badge on the video |
| ← / → | Jump to the previous or next line |


### Changing the model

Open **Transcription model** in the popup and type a name into **Model**.

More about models, Apple Silicon and the server's options: [server.md](docs/server.md). Every
popup setting is explained in [the guide](docs/guide.md#settings).

## Word colours

Two colourings, both optional and off by default, in the popup's **Word colours** section:

- **Colour words by their Anki card:** green for learned, yellow for learning, orange for
  suspended, red for new.
- **Overbar by pitch accent:** blue for heiban, red for atamadaka, orange for nakadaka, green for
  odaka.

The full rules: [Word colours](docs/guide.md#word-colours).

## Updating

Shisu-ko mostly keeps itself up to date. On every restart the server pulls the newest version. 

## Documentation

- [Installing, updating and removing Shisu-ko](docs/install.md)
- [Using Shisu-ko](docs/guide.md): every setting, sentence mining and the word colours in full,
  and live streams
- [The server](docs/server.md): its options, YouTube sign-in, Apple Silicon and the
  Kitsune-Transcribe models
- [AMD graphics cards](docs/amd.md) (experimental)
- [Troubleshooting](docs/troubleshooting.md)
- [How it works](docs/how-it-works.md) and [subtitle quality](docs/subtitle-quality.md)
- [The phone portal](portal/README.md)
- [Privacy policy](docs/amo/privacy-policy.md)

## Development

Bug reports and ideas are welcome as [issues](https://github.com/Multysquid/shisu-ko/issues).
[AGENTS.md](AGENTS.md) explains the architecture,
[docs/dev](docs/dev/README.md) has the design of each part.

`npm test` needs Node.js 22 or newer.

```
pip install -r server/requirements-test.txt
python -m pytest server/tests
python -m pytest portal/tests
npm ci
npm test
node --test portal/tests/lib.test.js
```

## Acknowledgements

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and
  [CTranslate2](https://github.com/OpenNMT/CTranslate2), and on Apple Silicon
  [MLX](https://github.com/ml-explore/mlx) with [mlx-whisper](https://pypi.org/project/mlx-whisper/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [PyAV](https://github.com/PyAV-Org/PyAV)
- OpenAI's [Whisper](https://github.com/openai/whisper) and
  [Kotoba-Whisper](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0)
- for the Kitsune models: [PyTorch](https://pytorch.org/),
  [transformers](https://github.com/huggingface/transformers), [librosa](https://librosa.org/),
  [Cohere Transcribe](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026) and NVIDIA's
  [Parakeet](https://huggingface.co/nvidia/parakeet-tdt_ctc-0.6b-ja)

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).

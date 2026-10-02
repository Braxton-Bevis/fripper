<p align="center">
  <img src="docs/media/banner.png" alt="FRipper: your music, one queue" width="100%">
</p>

<p align="center">
  <a href="../../releases/latest/download/FRipper.zip"><img alt="Download FRipper" src="https://img.shields.io/badge/Download-FRipper.zip-ffffff?style=for-the-badge&labelColor=0b0b0d"></a>
  <img alt="Windows 10/11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-17171a?style=for-the-badge&logo=windows&logoColor=white">
  <img alt="macOS 11+" src="https://img.shields.io/badge/macOS-11%2B-17171a?style=for-the-badge&logo=apple&logoColor=white">
  <a href="../../releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/Braxton-Bevis/fripper?style=for-the-badge&labelColor=0b0b0d&color=17171a"></a>
</p>

**FRipper** is a desktop app for saving music you have access to. Paste TIDAL, SoundCloud or other music links, build a queue, and download every track in its original quality.

<p align="center"><img src="docs/media/screenshot-queue.png" alt="FRipper downloading a playlist" width="88%"></p>

## How it works

<p align="center"><img src="docs/media/steps.png" alt="Paste links, build the queue, press start" width="100%"></p>

- **Paste anything:** playlists, albums or single tracks, one per line, or import a `.txt` file. FRipper detects TIDAL and SoundCloud links and picks the right mode.
- **Original quality by default:** source files are kept untouched. You can also convert to MP3, FLAC, AAC, Opus, OGG or WAV.
- **Live progress:** track-by-track progress, colour-coded status, and full job logs. Files are checked before a job is marked complete.
- **Paced and careful:** one TIDAL or SoundCloud transfer at a time, with a speed cap and a pause between tracks. Rate limits pause the queue automatically.
- **Private by design:** FRipper never asks for a password. TIDAL uses its official device sign-in, and sessions are encrypted with Windows' per-user protection.

## Install

1. Download **[FRipper.zip](../../releases/latest/download/FRipper.zip)**, then unzip it.
2. Run the installer for your computer:

| Computer | Run this | First-time note |
| --- | --- | --- |
| Windows 10/11 | `Install FRipper (Windows).cmd` | If SmartScreen appears, choose **More info → Run anyway**. |
| macOS 11+ | `Install FRipper (Mac).command` | Right-click it and choose **Open** (double-click is blocked the first time). Enter your Mac password if it asks to install Python. |

The installer gets everything FRipper needs (Python, the download engines and a private browser). This takes a few minutes the first time, and then FRipper opens.

**Keep it handy**
- **Windows:** FRipper is in the Start menu and on the Desktop. Right-click it, then choose **Pin to taskbar**.
- **Mac:** FRipper is in your Applications folder. While it's open, right-click its Dock icon and choose **Options → Keep in Dock**.

To update, download the new zip and run the installer again. Your queue, sign-ins and music are kept.

## Sources

| Source | Setup | Windows | Mac |
| --- | --- | :---: | :---: |
| SoundCloud | None | ✓ | ✓ |
| TIDAL (your subscription) | **Connect TIDAL**, then sign in to your own subscription in the browser | ✓ | — |
| Amazon Music, Qobuz, GrilledCheese (via Lucida) | **Set up Lucida** once | ✓ | ✓ |

Music is saved to `Music/FRipper` in your user folder unless you choose another folder.

<p align="center"><img src="docs/media/screenshot-welcome.png" alt="FRipper welcome screen" width="70%"></p>

## Share FRipper

Ready-made graphics are in [`docs/media`](docs/media):

<table>
  <tr>
    <td width="38%"><a href="docs/media/poster.png"><img src="docs/media/poster.png" alt="Social poster"></a><br><sub><b>Poster</b> · 1080×1350, for feeds</sub></td>
    <td width="24%"><a href="docs/media/story.png"><img src="docs/media/story.png" alt="Story"></a><br><sub><b>Story</b> · 1080×1920</sub></td>
    <td width="38%"><a href="docs/media/flyer.png"><img src="docs/media/flyer.png" alt="Printable flyer"></a><br><sub><b>Flyer</b> · US Letter, with a QR code</sub></td>
  </tr>
</table>

<p align="center"><a href="docs/media/brand.png"><img src="docs/media/brand.png" alt="FRipper brand sheet" width="100%"></a></p>

The artwork is generated from [`docs/design/artboards.html`](docs/design/artboards.html) (`python docs/design/render.py`). The app screenshots use demo data (`python docs/design/screenshots.py`).

## Good to know

- TIDAL sign-ins use Windows' per-user encryption, so TIDAL is Windows-only for now.
- Lucida availability depends on the Lucida service, independent of this app.
- Only download music you have the right to keep.

## Credits

FRipper is a Fund Ravers project. It's built on [lucidadl](https://github.com/Jude-A/lucidadl) (a community client, not affiliated with lucida.to), [tidalapi](https://github.com/tamland/python-tidal), [yt-dlp](https://github.com/yt-dlp/yt-dlp), FFmpeg and Mutagen. See `SOURCE.txt` for pinned versions and licenses.

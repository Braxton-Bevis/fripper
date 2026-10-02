# FRipper

A desktop app for saving music you have access to: paste TIDAL, SoundCloud or other music links, build a queue, and download them in their original quality.

## Install

1. Download **FRipper.zip** from the [latest release](../../releases/latest).
2. Unzip it, then open the **FRipper** folder.
3. Run the installer for your computer:

| Computer | Run this | First-time note |
| --- | --- | --- |
| Windows 10/11 | `Install FRipper (Windows).cmd` | If SmartScreen appears, choose **More info → Run anyway**. |
| macOS 11+ | `Install FRipper (Mac).command` | Right-click it and choose **Open** (double-click is blocked the first time). Enter your Mac password if it asks to install Python. |

The installer gets everything FRipper needs (Python, the download engines and a private browser). This takes a few minutes the first time, and then FRipper opens.

**Keep it handy**
- **Windows:** FRipper is in the Start menu and on the Desktop. Right-click it, then choose **Pin to taskbar**.
- **Mac:** FRipper is in your Applications folder. While it's open, right-click its Dock icon and choose **Options → Keep in Dock**.

To update, download the new zip and run the installer again. Your queue, sign-ins and music are kept.

## Use

1. **Paste links** into *Add music*, one per line. FRipper detects TIDAL and SoundCloud links and picks the right link type for you.
2. Choose an **audio source** and **format**. *Original quality* keeps the source file untouched.
3. Click **Add to queue**, then **Start queue**. You can watch progress in the toolbar, and job details show each track.

Double-click a job to open its folder. Right-click for more options.

| Source | Setup | Platforms |
| --- | --- | --- |
| SoundCloud | None | Windows, Mac |
| TIDAL account | **Connect TIDAL**, then sign in to your own subscription in the browser | Windows |
| Amazon Music, Qobuz, GrilledCheese (via Lucida) | **Set up Lucida** once | Windows, Mac |

Music is saved to `Music/FRipper` in your user folder unless you choose another folder.

## Good to know

- TIDAL sign-ins are encrypted with Windows' per-user protection, which is why TIDAL is Windows-only for now.
- FRipper never asks for a password. TIDAL uses the official device sign-in.
- Downloads are paced: one TIDAL or SoundCloud track at a time, with a speed cap and a pause between tracks.
- Only download music you have the right to keep.

## Credits

Built on [lucidadl](https://github.com/Jude-A/lucidadl) (community client, not affiliated with lucida.to), [tidalapi](https://github.com/tamland/python-tidal), [yt-dlp](https://github.com/yt-dlp/yt-dlp), FFmpeg and Mutagen. See `SOURCE.txt` for pinned versions and licenses.

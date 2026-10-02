FRIPPER

Open "Launch FRipper.vbs" for the desktop app. If it does not open, use
"Launch FRipper.cmd" to see startup errors. Dependencies are installed here;
"Install client.cmd" can recreate them with Python 3.10+ and Git.

FRipper uses the Fundravers FR mark and black-and-white branding.
Previous Launch Lucida shortcuts still work. The running window adopts the
updated interface on its next normal launch; downloads need not be stopped.

SOUNDCLOUD

Choose SoundCloud links, paste one track or set URL per line, Add to queue,
then Start queue. Public personalized set links are supported when SoundCloud
allows access. SoundCloud uses its own download route and does not need Lucida.

Keep "Original quality" to preserve the best file offered by SoundCloud.
An uploader-enabled original download is preferred. Otherwise the highest
available audio stream is saved, without pretending that it is the original.
The job log identifies the source used. Lossless originals and paid-account
stream quality are only available when the service actually offers them.
This app's SoundCloud route currently uses public access, without browser
cookies or a Go+ sign-in. It cannot unlock private or subscription-only media.

TIDAL ACCOUNT

Select Connect TIDAL and finish the official device sign-in in your browser.
The login link in Job details can also be opened on a phone. Approval connects
this desktop. If TIDAL blocks that page, the app cannot finish authentication;
there is no saved connection until the log reports "TIDAL connected".

Choose TIDAL account and paste TIDAL track, album or playlist links.
The direct route requests exact catalog audio using your subscription,
preferring normal lossless quality. It reports what the account actually
receives. It does not promise hi-res audio or decrypt protected streams.
TIDAL credentials are encrypted using Windows DPAPI for the current Windows
user in .local/tidal/session.bin. No account password is collected by the app.

LUCIDA SOURCES

The included Jude-A/lucidadl is a community client, not an official Lucida
application or a self-hosted copy of lucida.to. Its pinned source is in
vendor/lucidadl. Choose Set up Lucida, complete any browser challenge yourself,
then choose Amazon Music, Qobuz or GrilledCheese as the audio source.

These sources accept supported links and searches such as "Artist - Title".
TIDAL links can also supply metadata to search the selected Lucida source.
That route matches tracks on another provider; the TIDAL account source above
uses exact TIDAL audio. Lucida provider availability varies independently of
this app. Setup and Diagnostics can succeed while an audio provider is down.

QUEUE AND FILES

Paste one item per line or import a UTF-8 .txt file. Lines starting with #
are ignored. Preview playlist reads the track list without saving audio.
Source, quality, destination and parallel settings are saved with each job.

Jobs run sequentially. Direct TIDAL and SoundCloud use one transfer at a time,
a 256 KiB/s speed cap, and five-second track spacing. TIDAL API calls are
paced separately. Rate-limit responses stop the batch and pause the queue.
These limits reduce request pressure; they cannot guarantee account treatment.
Parallel controls transfers only for Lucida sources. Playlist order and
duplicate entries are retained, and direct routes create M3U8 playlists.
Incomplete playlists are explicitly marked partial; errors do not count as
completed jobs. Direct-route files are validated before being marked complete.

Original quality preserves source audio. MP3/AAC/FLAC/etc. convert locally;
conversion cannot improve the quality of the source. Keep original retains
the source file alongside a converted copy. Existing validated files can be
reused when a failed job is retried.

Pause finishes the current job before stopping. Stop current cancels it and
pauses the queue. Retry failed requeues failed/stopped/interrupted jobs.
Closing the app saves the queue and stops active work. It does not
automatically restart downloads the next time you open it.

Default downloads: downloads inside this project, changeable in the app.
Queue, settings, logs and saved access: .local inside this project.
This folder can contain listening history and saved session data; keep it
private. TIDAL sessions are tied to your Windows account.

DEVELOPMENT

GUI: gui.py (tkinter); queue/process control: manager.py
Direct routes: tidal_account.py, tidal_direct.py, soundcloud_direct.py
Lucida adapter: client_bridge.py, tidal_playlists.py, lucida_search.py
Tests: .venv\Scripts\python.exe -m unittest discover -s tests -v
Source details: SOURCE.txt; installed versions: installed-packages.txt

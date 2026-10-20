# claude-sopro-tts

Claude Code, out loud. When Claude finishes a turn, this reads the reply to you
with [Sopro TTS](https://github.com/samuel-vitorino/sopro) — a 120M-parameter
voice-cloning model running **locally on your CPU**. No API keys, no cloud, no
GPU.

It is a **Claude Code hook**: `install` registers it on `Stop` (speak the reply),
`UserPromptSubmit` (cut audio when you type), `SessionStart` (preload), and
`SessionEnd` (stop), so it fires automatically with nothing for the model to
remember to call.

Replies are read at **1.25×** by default, pitch preserved; `claude-tts speed 1.0`
gives the faithful pace.

```
you: "fix the auth refresh bug"
claude: (works, writes the fix)
you hear: "Fixed the refresh path in src/auth.py. Tests pass."
```

## Install

```bash
uvx --from git+https://github.com/marques576/claude-tts claude-tts install
```

That command is self-bootstrapping: it installs the package as a uv tool (so the
hooks keep a stable absolute path), writes the hooks and warms the model.

`install` does the following:

- installs `claude-tts` as a [uv tool](https://docs.astral.sh/uv/) so the hook
  keeps working after `uvx` exits (dependencies: `sopro`, `torch`, `sounddevice`),
- writes the hooks into `~/.claude/settings.json` (existing hooks are kept),
- starts the daemon and downloads the ~640 MB Sopro weights on first run,
- speaks with a bundled public-domain reference voice, so nothing else is needed.

Then start a **new** Claude Code session (or run `/hooks` once to reload) and it
just talks.

Requirements: macOS or Linux, Python 3.10–3.13 (uv fetches it), and ~2 GB of
disk.

Working on this repo? `uv run claude-tts install --reinstall` rebuilds the tool
from your working tree and picks up local edits. Audio plays through PortAudio when available, otherwise `afplay` /
`paplay` / `aplay` / `ffplay`.

### Options

```bash
claude-tts install --ref ~/my-voice.wav   # clone your own 5-20 s clip
claude-tts install --int8                 # faster CPU model (a bit less clean)
claude-tts install --project              # hook only for this repo (.claude/settings.json)
claude-tts install --no-warm              # skip the model download for now
```

## Everyday use

```bash
claude-tts status       # daemon, model, last reply timings
claude-tts off          # mute
claude-tts on           # unmute
claude-tts stop         # shut up mid-sentence (~0.2 s)
claude-tts speak "hello" # test the voice
claude-tts speed 1.0    # faithful pace (default is 1.25; 0.5-2.0, live)
claude-tts kill         # stop the daemon and free its ~1 GB of RAM
claude-tts voice        # which reference clip is used
claude-tts voice me.wav # switch reference clip (5-20 s of clean speech)
claude-tts doctor       # check hooks, audio device, model, daemon
```

Everything is configurable in `~/.config/claude-tts/config.json`:

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `true` | speak at all |
| `voice` | bundled LibriVox clip | reference audio path |
| `max_chars` | `1500` | speak the first N chars, then say "the rest is on screen" |
| `int8` | `false` | int8 AR weights, faster on CPU |
| `steps` | model default (2) | acoustic solver steps; higher = slower, cleaner |
| `lang` | auto | `en`, `pt`, `fr`, `de` hint |
| `speed` | `1.25` | playback rate, `0.5`-`2.0`, pitch preserved (WSOLA); `1.0` skips the time-stretcher |
| `volume` | `1.0` | playback gain |
| `port` | `45733` | local daemon port |
| `player` | `auto` | `auto`, `external`, or a command like `afplay` |

`claude-tts config max_chars 800` edits it for you (`none` clears a value). Changing the model,
`voice`, `int8`, or `device` needs `claude-tts restart`.

## What gets read

Only prose. Fenced code, diffs, tables, shell transcripts, URLs, HTML and
markdown decoration are stripped; inline code is kept (so file names are
audible). Replies are capped at `max_chars` on a sentence boundary and end with
"The rest is on screen."

A new turn immediately cuts the old reply (barge-in); speaking your next prompt
also cuts it.

## How it fits together

```
Claude Code ──Stop hook──▶ claude-tts hook ──HTTP──▶ daemon (model warm)
                                                      │
                                    Sopro TTS on CPU ─┴─▶ sounddevice / afplay
```

- `src/claude_tts/hook.py` — reads the hook payload (`last_assistant_message`,
  falling back to the transcript), cleans it, posts it, exits in ~50 ms.
- `src/claude_tts/daemon.py` — keeps `SoproTTS` resident, streams synthesis
  chunk-by-chunk, cancels on barge-in.
- `src/claude_tts/cleaner.py` — markdown → speech.
- `src/claude_tts/playback.py` — streaming output with graceful OS-player
  fallback.
- `src/claude_tts/installer.py` — uv tool install, hook merge, voice setup.

Logs: `~/.cache/claude-tts/daemon.log` and `hook.log`.

## Uninstall

```bash
claude-tts uninstall          # hooks + daemon, keeps config
claude-tts uninstall --purge  # also config, cache, the uv tool
```

## Credits & licence

- [Sopro](https://github.com/samuel-vitorino/sopro) by Samuel Vitorino (Apache-2.0) —
  the TTS model.
- The bundled reference voice is a public-domain LibriVox excerpt; see
  `src/claude_tts/assets/NOTICE.md`.
- This project: MIT.

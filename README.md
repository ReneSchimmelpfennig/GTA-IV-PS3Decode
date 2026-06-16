# GTA IV — PS3 Audio Mod

First off, let me be transparent and say that Claude (Opus 4.8) wrote the code, while I am just an orchestrator. Real modders, please don't see this as an attack on your work, I just started playing around with Claude (not "Speed", in fact it was quite a slow process), hours turned into days and days turned into weeks and suddenly I got a somehow working script, something I had never expected. Enjoy :)

Replace **GTA IV (PC)**'s software-decoded ADPCM audio with the **PS3 version's
MP3 audio**, for noticeably better-sounding radio, cutscenes and speech — without
re-encoding the audio.

The PC release stores most streamed/voice audio as IMA-ADPCM, decoded in software.
The PS3 release ships the same audio as MP3. This project swaps the MP3 payload
into the PC container (keeping the ADPCM container skeleton and `codec` tag intact)
and adds a small **ASI plugin** that hooks the game's software ADPCM decoder,
detects the smuggled-in MP3 by its frame sync, and decodes it with
[minimp3](https://github.com/lieff/minimp3).

> **You must own GTA IV and supply your own audio files.** This repository contains
> **only original code** — no game assets, no copyrighted audio, no game binaries.
> See [Legal / scope](#legal--scope).

---

## How it works

```
PS3 .ivaud (MP3)            PC .ivaud (IMA-ADPCM)
      │                            │
      │  extract per channel/sound │  keep container, headers,
      │  pack to rate-matched CBR  │  seek tables and codec=0x400
      ▼          (mp3packer)       ▼
      └──────────  payload swap  ──┘
                       │
                       ▼
            PC .ivaud with MP3 bytes
            in the ADPCM slots
                       │
       in-game │ codec 0x400 routes to the software
               │ decoder → our ASI hook fires
                       ▼
        hook sees the MP3 sync, decodes with minimp3,
        returns PCM in place of ADPCM
```

The trick that makes it seamless is **byte = time**: IMA-ADPCM at 4 bits/sample is
exactly 2 samples per byte, so a constant-bitrate MP3 whose byte rate equals the
ADPCM byte rate stays perfectly in sync with the game's streaming clock.

| sample rate | ADPCM byte rate | matching MP3 CBR |
|-------------|-----------------|------------------|
| 32 kHz      | 16000 B/s       | **128 kbps**     |
| 24 kHz      | 12000 B/s       | **96 kbps**      |

(general rule: `kbps = sample_rate × 4 / 1000`)

The MP3 is produced **losslessly** from the PS3 source with
[mp3packer](https://github.com/Reuben-Thorpe/mp3packer) (`-b <kbps> -r`), which
re-frames the existing MP3 data to constant bitrate without a re-encode.

---

## Status

| Part                         | State |
|------------------------------|-------|
| Cutscenes                    | ✅ working |
| Radio                        | ✅ working (≈32 ms warm-up tick on tune-in, see notes) |
| Banks                        | ✅ working |
| Radio "downgrader" (restoring removed Complete-Edition songs as MP3) | 📋 planned |

---

## Repository layout

```
asi/            The ASI plugin (C++/MinHook/minimp3)
  dllmain.cpp     production decoder (hook + MP3 detection + decode)
  diagnostics/    optional logging builds used while reverse-engineering
tools/          Python converters & analyzers
  gta4_ps3_audio.py   radio/cutscene pipeline (RPF in → swapped RPF out)
  bank_swap.py        bank pipeline (per sub-sound)
  analyze_packed.py   verify an MP3 is clean uniform CBR
  mp3_framemap.py     CBR/VBR frame analyzer
docs/
  FORMAT.md       container / bank format & key addresses
  BUILD.md        building the ASI
```

### Tools

These tools are included and used to create Frankenstein RPFs that contain PS3 audio but adhere to PC RPF standards:

- `tools/rpf3.py` — RPF3 reader/writer + AES key extraction
- `tools/rage_aud_deinterleave.py` — PS3 ivaud → per-channel mono MP3
- `tools/ivaud_payloadswap.py` — byte-exact payload swap
- `tools/hashes.txt` — RPF name-hash table

Third-party dependencies you must place in `asi/` to build:

- [`minimp3.h`](https://github.com/lieff/minimp3) (CC0 / public domain)
- [MinHook](https://github.com/TsudaKageyu/minhook) sources (BSD-2-Clause)

See [`THIRD_PARTY.md`](THIRD_PARTY.md).

---

## Quick start

### 1. Build the ASI
See [`docs/BUILD.md`](docs/BUILD.md). Result: `GTA4MP3.asi` → drop into
`...\Grand Theft Auto IV\GTAIV\plugins\` (requires an ASI loader).
Compiled ASI can be found in releases. It includes the actual hook and PS3 decoder for the game.

### 2. Convert PS3 RPFs into a format the PC version can read (lossless process)
```
py tools/gta4_ps3_audio.py <GTAIV.exe> <pc.rpf> <ps3.rpf> -o <out.rpf> \
   --mp3packer C:\path\mp3packer.exe
# batch over folders:
py tools/gta4_ps3_audio.py <pc_dir> <ps3_dir> -o <out_dir> --batch --mp3packer ...
```

---

## Notes & limitations

- **Radio tune-in tick (~32 ms).** Entering an MP3 stream mid-song means the first
  frame references the bit reservoir of frames you didn't load, plus the first
  streaming chunk is one fractional frame short. This is inherent to mid-stream MP3
  entry; the decoder is already at the practical floor (see `MARGIN` in `dllmain.cpp`).
- **Speech length.** PS3 voicelines can be a few tens of ms longer than the PC slot;
  the overflow is truncated (usually trailing silence/decay, inaudible).
- **PCM banks are kept.** PC PCM (`codec 0x1`) is lossless and already better than the
  PS3 MP3 equivalent, so the bank converter skips anything that isn't ADPCM
  (`codec 0x400`).
- Tested against **GTAIV.exe v1.2.0.59** (Steam Complete Edition). Other versions need
  the RVAs in `docs/FORMAT.md` re-checked.

---

## Legal / scope

This is a personal, single-player audio mod. It ships **source code only**. It does
**not** contain or distribute any Rockstar/Take-Two assets, audio, or executables.
To use it you must legally own GTA IV (PC) and provide the PS3 audio files yourself.
Reverse-engineered format notes are included for interoperability so that the tools
can read/write the audio containers. Use at your own risk; this project is not
affiliated with or endorsed by Rockstar Games or Take-Two.

## License


Original code in this repository is released under the [MIT License](LICENSE). Bundled/required third parties keep their own licenses —
see [`THIRD_PARTY.md`](THIRD_PARTY.md).

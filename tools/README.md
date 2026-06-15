# Converters

Python 3 tools that build the swapped audio. They call **mp3packer** for the
lossless VBR→CBR step (`--mp3packer <path>` or have it on PATH) and need
`pycryptodome` for RPF AES (`py -m pip install --user pycryptodome`).

## Core modules (included)
- `rpf3.py` — RPF3 reader/writer + AES key extraction
- `rage_aud_deinterleave.py` — PS3 ivaud → per-channel mono MP3
- `ivaud_payloadswap.py` — byte-exact payload swap

You still supply yourself:
- `hashes.txt` — RPF name-hash table (not redistributed here)
- `pyrpfiv` (optional) — for hash→name resolution; without it, files show as `hash_XXXXXXXX`

## Scripts
- `gta4_ps3_audio.py` — main RPF→RPF pipeline (`--batch`, `--dry-run`). Swaps
  streamed radio/cutscene ivauds AND auto-detects non-streamed speech/pain banks,
  routing them through `bank_swap` (grow mode). One pass does everything in an RPF.
- `bank_swap.py` — bank swap, also usable standalone. Default = **grow mode**:
  rebuilds the data region so each sound fits its whole MP3 (no tail cut); the
  leading Info/Xing frame is stripped. `--slot` is the simpler v1 (write into the
  fixed PC slot, truncate the tail; warns if a PS3 line is longer than its slot).
- `analyze_packed.py` — confirm an MP3 is clean uniform CBR (no gaps)
- `mp3_framemap.py` — CBR/VBR frame analyzer

CBR rate is derived per sound from the sample rate (`kbps = rate × 4 / 1000`):
32 kHz → 128, 24 kHz → 96. PCM banks (`codec 0x1`) are intentionally left alone.

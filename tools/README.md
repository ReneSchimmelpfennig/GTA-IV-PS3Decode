# Converters

Python 3 tools that build the swapped audio. They call **mp3packer** for the
lossless VBR→CBR step (`--mp3packer <path>` or have it on PATH) and need
`pycryptodome` for RPF AES (`py -m pip install --user pycryptodome`).

## Core modules (included)
- `rpf3.py` — RPF3 reader/writer + AES key extraction
- `rage_aud_deinterleave.py` — PS3 ivaud → per-channel mono MP3
- `ivaud_payloadswap.py` — byte-exact payload swap
- `mp3packer.exe` — losslessly repacks PS3 MP3's so that they are CBR instead of VBR --> [source](https://hydrogenaudio.org/index.php/topic,32379.0.html), credit user "Omion"
- `hashes.txt` — RPF name-hash table

You still supply yourself:
- `pyrpfiv` (optional) — for hash→name resolution; without it, files show as `hash_XXXXXXXX`

## Scripts
- `gta4_ps3_audio.py` — main RPF→RPF pipeline (`--batch`, `--dry-run`). Swaps
  streamed radio/cutscene ivauds AND auto-detects non-streamed audio banks,
  routing them through `bank_swap` (grow mode). One pass does everything in an RPF.
- `bank_swap.py` — bank swap, also usable standalone. Default = **grow mode**:
  rebuilds the data region so each sound fits its whole MP3 (no tail cut); the
  leading Info/Xing frame is stripped. `--slot` is the simpler v1 (write into the
  fixed PC slot, truncate the tail; warns if a PS3 line is longer than its slot).
- `analyze_packed.py` — confirm an MP3 is clean uniform CBR (no gaps)
- `mp3_framemap.py` — CBR/VBR frame analyzer
- `slotcheck.py` — QA a converted RPF: flags any swapped bank sound that would play as
  noise — checks the first frame fits the slot, the sync chain is intact, and the rate is
  consistent. Pass the original RPF as a 3rd arg to byte-diff each slot, which separates a
  genuinely broken swap from an untouched ADPCM sound:
  `py slotcheck.py <GTAIV.exe> <converted.rpf> [original.rpf]`

CBR rate is derived per sound from the sample rate (`kbps = rate × 4 / 1000`):
32 kHz → 128, 24 kHz → 96. PCM banks (`codec 0x1`) are intentionally left alone.

# Format & reverse-engineering notes

Verified against **GTAIV.exe v1.2.0.59**, ImageBase `0x400000`.
Runtime addresses are ASLR-shifted: `runtime = RVA + module_base`.

## Key functions (RVA)

| RVA        | Function | Notes |
|------------|----------|-------|
| `0x497440` | `audIMA_ADPCM_Decode` | the software ADPCM decoder — **this is what we hook** |
| `0x48F0D0` | `audVoicePcAdpcm::Init` | `__thiscall(voice, p2)`; ADPCM state at `voice+0x14E` (per-voice hook key). Calls the decoder for the first block → hook fires here too |
| `0x48EE70` | `audVoicePcAdpcm_DecodeDriver` | reads `dataBase` linearly; feeds fixed `0x4000` chunks (`0x8000` on the first call) |
| `0x8871B0` | voice factory | codec bit `0x400` → `audVoicePcAdpcm` (software path). PC has no MPEG decoder, so the ivaud must keep `codec=0x400`; MP3 is detected by data sniff |

### Decoder calling convention
`__fastcall(ecx = state = voice+0x14E, edx = unused, outPcm, inData, 0, byteCount)`.
Input is `byteCount` **bytes**; output is `2 × byteCount` **samples** (IMA = 2 samples/byte).

## Streamed container (radio / cutscene)

Header:

| off | type | field |
|-----|------|-------|
| 0x00 | u64 | table_off |
| 0x08 | u32 | blocks |
| 0x0c | u32 | block_chunk |
| 0x10 | u32 | streamCount (0 = streamed) |
| 0x14 | u64 | channel-table off |
| 0x24 | u32 | channels |
| 0x28 | u32 | hasTimestamps |
| 0x2c | u32 | dataOffset |

Per block (header at `dataOffset + i × block_chunk`): base header `3 × u64`
(`0x18`, then channel-info offset, then seek-table offset), channel-info
**0x10/channel** for PC-ADPCM (`start_entry`, `entries`, `skip`, `sampleCount`;
PS3-MPEG uses 0x18/channel), then seek tables **0x08/entry**.
`data_start = block_off + align(header_size, 0x800)`. Channel payload at
`data_start + start_entry × 0x800`, length `entries × 0x800`. Channels are
contiguous (not frame-interleaved), `skip = 0`. Timestamps live separately and do
**not** move `data_start`.

## Non-streamed container (banks: speech, pain, radio DJ announcements)

Header: `table = u64@0x00`, `total = u32@0x10` (sub-sound count),
`base = u32@0x18`. **PC is little-endian, PS3 is big-endian.**

Sub-sound table at `table`: per sound a `u64 @ table + i×0x10` → offset to its
info struct at `sio + off`, where `sio = table + total×0x10`.

Info struct (0x20 bytes):

| off | type | field |
|-----|------|-------|
| +0x00 | u64 | data offset (relative to `base`) |
| +0x08 | u32 | **name hash** (match PC ↔ PS3 by this, not by index) |
| +0x0c | u32 | size (bytes) |
| +0x10 | u32 | nsamp |
| +0x18 | u16 | sample rate |
| +0x1c | u32 | codec (`0x400` = ADPCM/software, `0x100` = MPEG, `0x1` = PCM) |

Audio data at `base + data_offset`, length `size`. `nsamp = 2 × size` (4-bit IMA).
Multiple sounds can share one data region (aliases) — same hash, same data offset;
swap once. PC and PS3 may list sounds in different order, so always match by hash.

## The decoder insight (why naïve streaming dropped audio)

The MP3, the swapped container, and the game's feed are all clean and contiguous
(verified byte-for-byte and with upstream minimp3). The dropouts came from
**minimp3 itself when fed in chunks**: if you let it decode the *last* frame in the
buffer before the *next* frame's header has arrived, it advances `frame_bytes` but
returns **0 samples** (it can't validate the frame), losing ~1 frame per chunk
boundary (~3 % silence).

Fix: keep a small look-ahead (`MARGIN`) and don't touch the last frame until enough
bytes are buffered; decode all complete frames into a FIFO and emit the requested
count from it. With strict CBR (byte = time) this leaves only an inherent ~1-frame
warm-up when entering a stream mid-song.

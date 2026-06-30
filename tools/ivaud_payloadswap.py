"""
ivaud_payloadswap.py
====================
Core of the PS3 MP3 mod: keeps the complete PC ADPCM ivaud skeleton (block
headers, seek tables, channel info -- everything Init/streaming needs) and
replaces ONLY the audio payload bytes per channel with MP3.

The ASI decoder hook detects the MP3 by its sync (FF Ex) and decodes it; the
container codec stays 0x400 (ADPCM routing) so the voice runs through the hook.

Functions:
  is_swappable(pc_bytes)            -> bool   (streamed ADPCM ivaud with block structure?)
  payload_swap(pc_bytes, mp3_list, target_rate=None) -> (new_bytes, info)
"""
import struct

def _ru16(d, o): return struct.unpack_from('<H', d, o)[0]
def _ru32(d, o): return struct.unpack_from('<I', d, o)[0]
def _ru64(d, o): return struct.unpack_from('<Q', d, o)[0]
def _rs32(d, o): return struct.unpack_from('<i', d, o)[0]
def _align(v, a): return (v + a - 1) // a * a

FRAME = 0x800


def is_swappable(pc):
    """True if pc is a streamed ADPCM ivaud with the expected block structure."""
    if len(pc) < 0x30:
        return False
    try:
        stream_count = _ru32(pc, 0x10)
        channels     = _ru32(pc, 0x24)
        data_off     = _ru32(pc, 0x2c)
    except struct.error:
        return False
    if stream_count != 0:          # 0 = streamed; otherwise a bank
        return False
    if channels < 1 or channels > 8:
        return False
    if data_off + 0x10 > len(pc):
        return False
    # base block header: u64=0x18, then u64=0x18+channels*0x10
    if _ru64(pc, data_off) != 0x18:
        return False
    if _ru64(pc, data_off + 8) != 0x18 + channels * 0x10:
        return False
    # CODEC GATE: only ADPCM (0x400) is decoded by the software path we hook. A PCM (or
    # any non-0x400) stream can share the exact same block structure, so the structural
    # checks above are NOT enough. If we swapped MP3 into a PCM container, its codec would
    # stay PCM and the PCM decoder would play the MP3 bytes as raw samples -> noise (the
    # LOADINGTUNE_1 @32kHz bug). The codec lives in the channel-info struct at
    # channel_table_offset(@0x14) + channels*0x10 + 0x1c (same field deint reads as 0x0100
    # for MPEG). Require 0x400 here so PCM/other streams are left untouched.
    try:
        ch_info = _ru64(pc, 0x14) + channels * 0x10
        if ch_info + 0x20 > len(pc):
            return False
        if _ru32(pc, ch_info + 0x1c) != 0x400:
            return False
    except struct.error:
        return False
    return True


def is_mp3_stream(b):
    """Does the byte stream start with an MPEG audio sync (FF Ex)?"""
    return len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0


def payload_swap(pc_in, mp3_list, target_rate=None):
    """
    pc_in       : bytes of the PC ADPCM ivaud (skeleton)
    mp3_list    : list of per-channel MP3 byte streams (order = channel order)
    target_rate : optional sample rate to patch into the main header + channel
                  infos (e.g. the PS3 rate, if != PC rate)

    Returns: (new_bytes, info_dict)
       info_dict: channels, written[], capacity[], complete(bool), rate_patched
    """
    pc = bytearray(pc_in)
    table_off = _ru64(pc, 0x00)
    blocks    = _ru32(pc, 0x08)
    bchunk    = _ru32(pc, 0x0c)
    ch_tab    = _ru64(pc, 0x14)
    channels  = _ru32(pc, 0x24)
    data_off  = _ru32(pc, 0x2c)

    if len(mp3_list) != channels:
        raise ValueError("channel count mismatch: ivaud=%d, MP3 streams=%d"
                         % (channels, len(mp3_list)))

    written = [0] * channels
    capacity = [0] * channels

    for bi in range(blocks):
        bo = data_off + bi * bchunk
        if bo + 0x18 > len(pc):
            break
        # channel info (0x10 per channel)
        off = bo + 0x18
        info = []
        for c in range(channels):
            se = _rs32(pc, off + 0x00)
            en = _rs32(pc, off + 0x04)
            info.append((se, en))
            off += 0x10
        # skip seek tables (entries * 0x08 per channel)
        for c in range(channels):
            off += info[c][1] * 0x08
        ds = bo + _align(off - bo, FRAME)
        # fill payload per channel sequentially
        for c in range(channels):
            se, en = info[c]
            po = ds + se * FRAME
            psize = en * FRAME
            capacity[c] += psize
            if po + psize > len(pc):
                psize = max(0, len(pc) - po)
            src = mp3_list[c]
            take = min(psize, len(src) - written[c])
            if take > 0:
                pc[po:po + take] = src[written[c]:written[c] + take]
                if take < psize:
                    pc[po + take:po + psize] = b'\x00' * (psize - take)
                written[c] += take
            else:
                pc[po:po + psize] = b'\x00' * psize

    rate_patched = False
    if target_rate:
        # Per-block table at table_off: pairs (start_sample u32, rate u32), one entry
        # per block. EVERY entry carries the rate - patching only the first (table_off
        # +0x04) leaves blocks 1..n at the old rate, so the stream's timeline desyncs
        # after block 0 and the channels scramble. Patch every block's rate field.
        # start_sample stays untouched (it is a sample position, rate-independent).
        for i in range(blocks):
            struct.pack_into('<I', pc, table_off + i*8 + 0x04, int(target_rate))
        # per channel: channel_info_off = ch_tab + channels*0x10 + rel
        for c in range(channels):
            rel = _ru64(pc, ch_tab + c * 0x10)
            info_off = ch_tab + channels * 0x10 + rel
            if info_off + 0x1a <= len(pc):
                struct.pack_into('<H', pc, info_off + 0x18, int(target_rate))
        rate_patched = True

    complete = all(written[c] >= len(mp3_list[c]) for c in range(channels))
    info = dict(channels=channels, written=written, capacity=capacity,
                complete=complete, rate_patched=rate_patched)
    return bytes(pc), info

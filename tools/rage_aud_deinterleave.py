#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rage_aud_deinterleave.py

Deinterleaves a DECRYPTED PS3 ivaud (RAGE AUD, codec 0x0100 = MPEG) into
per-channel raw MP3 streams. LOSSLESS: only bytes are reordered, nothing is
re-encoded.

Ported from vgmstream (rage_aud.c + rage_aud_streamfile.h).

Input:  a decrypted ivaud file (as it sits in the RPF data area; your existing
        script can extract it).
Output: <name>_ch0.mp3, <name>_ch1.mp3, ...

Test afterwards: the _chN.mp3 should play directly in an MP3 player (clean mono).
                If they do, the deinterleaving is correct.

IMPORTANT: this expects the streaming/music variant (is_streamed). Banks (SFX)
           are laid out differently and are not covered here.
"""

import sys
import struct

FRAME_SIZE = 0x800  # RAGE_AUD_FRAME_SIZE


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


class Reader:
    """Endian-aware reader over a bytes buffer."""
    def __init__(self, data, big_endian):
        self.d = data
        self.be = big_endian

    def u16(self, off):
        return struct.unpack_from('>H' if self.be else '<H', self.d, off)[0]

    def u32(self, off):
        return struct.unpack_from('>I' if self.be else '<I', self.d, off)[0]

    def s32(self, off):
        return struct.unpack_from('>i' if self.be else '<i', self.d, off)[0]

    def u64(self, off):
        return struct.unpack_from('>Q' if self.be else '<Q', self.d, off)[0]


# ----------------------------------------------------------------------------
#  Parse the header (streaming variant)
# ----------------------------------------------------------------------------
def parse_header(data):
    # big_endian: on PS3 the 64-bit table offset is BE, i.e. the top 4 bytes
    # (read_u32be(0x00)) are 0.
    big_endian = (struct.unpack_from('>I', data, 0x00)[0] == 0)
    r = Reader(data, big_endian)

    table_offset = r.u64(0x00)
    if table_offset > 0x20000 or table_offset < 0x1c:
        raise ValueError("Implausible table_offset 0x%X - not a valid ivaud?" % table_offset)

    is_streamed = (r.u32(0x10) == 0)
    if not is_streamed:
        raise ValueError("Bank/SFX ivaud (not streamed) - not supported here.")

    block_count          = r.u32(0x08)
    block_chunk          = r.u32(0x0c)
    channel_table_offset = r.u64(0x14)
    channels             = r.u32(0x24)
    stream_offset        = r.u32(0x2c)

    channel_info_offset = channel_table_offset + channels * 0x10
    num_samples = r.u32(channel_info_offset + 0x10)
    sample_rate = r.u32(block_table_offset_field := table_offset + 0x04)  # block_table[+0x04]
    codec       = r.u32(channel_info_offset + 0x1c)

    if codec != 0x0100:
        raise ValueError("Codec 0x%X is not MPEG(0x0100) - this tool is PS3-MP3 only." % codec)

    return {
        'big_endian': big_endian,
        'block_count': block_count,
        'block_chunk': block_chunk,
        'channels': channels,
        'stream_offset': stream_offset,
        'num_samples': num_samples,
        'sample_rate': sample_rate,
        'codec': codec,
        'reader': r,
    }


# ----------------------------------------------------------------------------
#  MP3 frame size from a 4-byte header (for repeat-skip detection)
#  Layer III only, which is what RAGE uses. Simplified, robust variant.
# ----------------------------------------------------------------------------
_BITRATE_V1_L3 = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
_BITRATE_V2_L3 = [0, 8,16,24,32,40,48,56, 64, 80, 96,112,128,144,160,0]
_SRATE_V1 = [44100,48000,32000,0]
_SRATE_V2 = [22050,24000,16000,0]
_SRATE_V25= [11025,12000, 8000,0]

def mp3_frame_size(hdr4):
    """Returns the frame size in bytes, or 0 if it is not a valid frame."""
    if len(hdr4) < 4:
        return 0
    b0, b1, b2, b3 = hdr4[0], hdr4[1], hdr4[2], hdr4[3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return 0
    version_id = (b1 >> 3) & 0x3   # 0=MPEG2.5,1=res,2=MPEG2,3=MPEG1
    layer      = (b1 >> 1) & 0x3   # 1=Layer III
    if version_id == 1 or layer == 0:
        return 0
    if layer != 0b01:  # Layer III only
        return 0
    br_index = (b2 >> 4) & 0xF
    sr_index = (b2 >> 2) & 0x3
    padding  = (b2 >> 1) & 0x1
    if br_index == 0 or br_index == 0xF or sr_index == 0x3:
        return 0
    if version_id == 3:      # MPEG1
        bitrate = _BITRATE_V1_L3[br_index] * 1000
        srate   = _SRATE_V1[sr_index]
        samples_per_frame = 1152
    elif version_id == 2:    # MPEG2
        bitrate = _BITRATE_V2_L3[br_index] * 1000
        srate   = _SRATE_V2[sr_index]
        samples_per_frame = 576
    else:                    # MPEG2.5
        bitrate = _BITRATE_V2_L3[br_index] * 1000
        srate   = _SRATE_V25[sr_index]
        samples_per_frame = 576
    if bitrate == 0 or srate == 0:
        return 0
    return (samples_per_frame // 8 * bitrate) // srate + padding


# ----------------------------------------------------------------------------
#  Read one block: per channel chunk_start, channel_size, channel_skip
# ----------------------------------------------------------------------------
def read_block(data, r, block_offset, channels):
    CHANNEL_ENTRY = 0x18   # MPEG
    SEEK_ENTRY    = 0x08

    blocks = []
    offset = block_offset + 0x18   # skip the base header

    # channel info table
    for ch in range(channels):
        start_entry     = r.s32(offset + 0x00)
        entries         = r.s32(offset + 0x04)
        channel_skip    = r.s32(offset + 0x08)
        channel_samples = r.s32(offset + 0x0c)
        channel_size    = r.s32(offset + 0x14)
        blocks.append({
            'start_entry': start_entry,
            'entries': entries,
            'channel_skip': channel_skip,
            'channel_samples': channel_samples,
            'channel_size': channel_size,
        })
        offset += CHANNEL_ENTRY

    # skip the seek table
    for ch in range(channels):
        offset += blocks[ch]['entries'] * SEEK_ENTRY

    header_size = offset - block_offset
    data_start  = block_offset + align_up(header_size, FRAME_SIZE)
    header_chunk = data_start - block_offset

    for ch in range(channels):
        blocks[ch]['chunk_start'] = header_chunk + blocks[ch]['start_entry'] * FRAME_SIZE

    return blocks, header_size


def get_repeat_size(data, block_offset, blk):
    """Repeated sub-frames at the block start (channel_skip!=0) -> bytes to skip."""
    if blk['channel_skip'] == 0:
        return 0
    base = block_offset + blk['chunk_start']
    frame = data[base:base + FRAME_SIZE]
    skip = 0
    while skip < len(frame) - 0x04:
        if frame[skip] == 0x00:          # padding -> whole frame repeated
            return FRAME_SIZE
        fsize = mp3_frame_size(frame[skip:skip+4])
        if fsize == 0:
            return FRAME_SIZE
        if skip + fsize > FRAME_SIZE:    # would cross the frame boundary -> no repeat
            return skip
        skip += fsize
    return skip


# ----------------------------------------------------------------------------
#  Main logic: concatenate all blocks per channel
# ----------------------------------------------------------------------------
def deinterleave(data):
    h = parse_header(data)
    r = h['reader']
    channels   = h['channels']
    block_chunk= h['block_chunk']
    stream_off = h['stream_offset']
    block_count= h['block_count']

    print("ivaud: %s-endian, channels=%d, sample_rate=%d, num_samples=%d, "
          "blocks=%d, block_chunk=0x%X, stream_offset=0x%X"
          % ('big' if h['big_endian'] else 'little', channels, h['sample_rate'],
             h['num_samples'], block_count, block_chunk, stream_off))

    out = [bytearray() for _ in range(channels)]

    # block_count may differ slightly per vgmstream; we iterate over the data area.
    block_offset = stream_off
    total = len(data)
    bi = 0
    while block_offset < total:
        blocks, _ = read_block(data, r, block_offset, channels)
        for ch in range(channels):
            blk = blocks[ch]
            repeat = get_repeat_size(data, block_offset, blk)
            start  = block_offset + blk['chunk_start'] + repeat
            size   = blk['channel_size'] - repeat
            if size > 0:
                out[ch] += data[start:start + size]
        bi += 1
        block_offset += block_chunk

    print("Processed blocks: %d" % bi)
    for ch in range(channels):
        print("  channel %d: %d bytes of raw MP3 stream" % (ch, len(out[ch])))
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: python rage_aud_deinterleave.py <decrypted.ivaud> [output_prefix]")
        sys.exit(1)
    inpath = sys.argv[1]
    prefix = sys.argv[2] if len(sys.argv) > 2 else inpath.rsplit('.', 1)[0]

    with open(inpath, 'rb') as f:
        data = f.read()

    channels = deinterleave(data)
    for ch, stream in enumerate(channels):
        outpath = "%s_ch%d.mp3" % (prefix, ch)
        with open(outpath, 'wb') as f:
            f.write(stream)
        print("written: %s" % outpath)
    print("\nTest: open the _chN.mp3 in an MP3 player - they should sound clean (mono).")


if __name__ == '__main__':
    main()

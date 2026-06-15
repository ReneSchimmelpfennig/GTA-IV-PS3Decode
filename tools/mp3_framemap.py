#!/usr/bin/env python3
# mp3_framemap.py
# Analyzes 1 or 2 mono MP3 streams (= deinterleaved PS3 radio channels, the
# output of rage_aud_deinterleave.py) and answers the Fix #6 question:
#
#   - CBR or VBR? (bitrate distribution)
#   - With 2 files: are the channels FRAME-SYNCHRONOUS?
#     (same frame count, same cumulative byte offsets of the frame boundaries)
#
# Result:
#   CBR + synchronous  -> Fix #6 can anchor both channels to the same frame via
#                         the shared readPos (simple, robust).
#   VBR / asynchronous -> Fix #6 needs an absolute anchor (frame 0 / timestamp).
#
# Usage:  py mp3_framemap.py channelL.mp3 [channelR.mp3]

import sys

# MPEG-1 Layer III bitrates (kbps), index 0=free/reserved
BR_MPEG1_L3 = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
# MPEG-2/2.5 Layer III
BR_MPEG2_L3 = [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0]
SR = {
    3: [44100,48000,32000,0],   # MPEG-1
    2: [22050,24000,16000,0],   # MPEG-2
    0: [11025,12000,8000,0],    # MPEG-2.5
}

def parse_frames(data):
    frames = []   # (offset, bitrate_kbps, samplerate, frame_bytes, samples)
    i = 0
    n = len(data)
    # skip ID3v2
    if n > 10 and data[:3] == b'ID3':
        size = ((data[6]&0x7f)<<21)|((data[7]&0x7f)<<14)|((data[8]&0x7f)<<7)|(data[9]&0x7f)
        i = 10 + size
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i+1] & 0xE0) != 0xE0:
            i += 1
            continue
        h = (data[i]<<24)|(data[i+1]<<16)|(data[i+2]<<8)|data[i+3]
        ver = (h>>19)&3      # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (h>>17)&3    # 1=Layer III
        br_i = (h>>12)&0xF
        sr_i = (h>>10)&3
        pad = (h>>9)&1
        if layer != 1 or ver == 1 or br_i in (0,15) or sr_i == 3:
            i += 1
            continue
        br = (BR_MPEG1_L3 if ver==3 else BR_MPEG2_L3)[br_i]
        sr = SR[ver][sr_i]
        if br == 0 or sr == 0:
            i += 1
            continue
        if ver == 3:
            samples = 1152
            fbytes = (144*br*1000)//sr + pad
        else:
            samples = 576
            fbytes = (72*br*1000)//sr + pad
        if fbytes < 4:
            i += 1
            continue
        frames.append((i, br, sr, fbytes, samples))
        i += fbytes
    return frames

def summarize(name, frames):
    if not frames:
        print(f"  {name}: NO valid frames found")
        return None
    brs = {}
    samp = 0
    for (_,br,sr,fb,s) in frames:
        brs[br] = brs.get(br,0)+1
        samp += s
    sr0 = frames[0][2]
    dur = samp / sr0
    cbr = len(brs) == 1
    print(f"  {name}: {len(frames)} frames | SR={sr0} Hz | duration={dur:.2f}s")
    print(f"    bitrates: " + ", ".join(f"{k}kbps x{v}" for k,v in sorted(brs.items())))
    print(f"    -> {'CBR' if cbr else 'VBR'}")
    return frames

def compare(fa, fb):
    print("\n[COMPARE L/R]")
    print(f"  frame count: L={len(fa)}  R={len(fb)}  ->",
          "EQUAL" if len(fa)==len(fb) else "DIFFERENT")
    nmin = min(len(fa), len(fb))
    # do the cumulative byte offsets of the frame boundaries match?
    mism = 0
    first_mismatch = None
    for k in range(nmin):
        if fa[k][0] != fb[k][0]:
            mism += 1
            if first_mismatch is None:
                first_mismatch = (k, fa[k][0], fb[k][0])
    if mism == 0 and len(fa)==len(fb):
        print("  frame boundaries byte-identical -> CHANNELS ARE FRAME-SYNCHRONOUS")
        print("  => Fix #6 SIMPLE: anchor both channels via the shared readPos.")
    else:
        print(f"  frame boundaries diverge (first divergence: {first_mismatch})")
        print(f"  diverging boundaries: {mism}/{nmin}")
        print("  => Fix #6 needs an ABSOLUTE anchor (frame 0 / radio timestamp).")

def main():
    if len(sys.argv) < 2:
        print("usage: py mp3_framemap.py channelL.mp3 [channelR.mp3]")
        return
    print("[FRAME MAP]")
    fa = parse_frames(open(sys.argv[1],'rb').read())
    fa = summarize(sys.argv[1], fa)
    fb = None
    if len(sys.argv) >= 3:
        fb = parse_frames(open(sys.argv[2],'rb').read())
        fb = summarize(sys.argv[2], fb)
    if fa and fb:
        compare(fa, fb)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bank_swap.py - payload swap for GTA IV non-streamed BANKS (speech.rpf, radio_x.rpf
announcements, pain/voice banks). Replaces the PC ADPCM sub-sounds with the PS3 MP3
counterparts (packed to a rate-matched CBR); container/header/codec stay untouched
-> routing through the software decoder (our ASI hook) is preserved.

DEFAULT = grow mode (the version that sounds best in practice):
  Rebuild the audio data region so each swapped sound gets a slot the exact size of
  its MP3 (the rate-matched CBR is typically ~1-2 frames larger than the original PC
  slot). The leading Info/Xing header frame is stripped (silent, lossless) and any
  trailing non-frame bytes trimmed, so the full audio plays and the tail is never cut.
  size/nsamp/data_off are patched; the header, sound table and info-struct region
  (everything before `base`) keep their positions. The whole bank is relaid out, so
  the output file is a bit larger than the input.

  --match-length (off by default): also pad each sound with silence up to the PS3
  length. Only useful if the PS3 lines are longer than the MP3; on these assets the
  PS3 lines are actually shorter, so it is a no-op there and best left off.

--slot (v1): write the MP3 straight into the fixed PC slot, size/nsamp UNTOUCHED,
  oversize (silent) tail truncated, short padded. Simpler, keeps PC pacing, but cuts
  real audio if a PS3 line is longer than its PC slot (a WARNING is logged then).

Bank format (verified):
  Header : table=u64@0x00, total=u32@0x10, base=u32@0x18  (PC little-, PS3 big-endian)
  Table  : per sound u64 @ table+i*0x10 -> info @ sio+off, sio=table+total*0x10
  Info   : data_off=u64@+0x00 (rel. base), name_hash=u32@+0x08, size=u32@+0x0c,
           nsamp=u32@+0x10, rate=u16@+0x18, codec=u32@+0x1c
  Audio  : base+data_off, length size.  nsamp=2*size (4-bit IMA, 2 samples/byte).
PC<->PS3 matching is by name_hash (not by index!). Aliases (several sounds sharing
the same data_off) are swapped only once.
"""
import struct, sys, os, subprocess, tempfile, argparse, math

# valid MPEG bitrate (kbps) for byte=time: ADPCM byte rate = rate/2 -> kbps = rate*4/1000
MP3_BITRATES = {32,40,48,56,64,80,96,112,128,160,192,224,256,320}
def rate_to_kbps(rate):
    if rate <= 0 or (rate*4) % 1000 != 0:
        return None
    k = rate*4//1000
    return k if k in MP3_BITRATES else None

# --- MPEG-1/2 Layer III frame length + Info/Xing header detection ---
_BR_V1 = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
_BR_V2 = [0, 8,16,24,32,40,48,56, 64, 80, 96,112,128,144,160,0]
_SR    = {3:[44100,48000,32000,0], 2:[22050,24000,16000,0], 0:[11025,12000,8000,0]}

def frame_len(b, i=0):
    """Byte length of the Layer III frame at b[i], or 0 if it is not a valid frame."""
    if i+4 > len(b):
        return 0
    if b[i] != 0xFF or (b[i+1] & 0xE0) != 0xE0:
        return 0
    ver = (b[i+1] >> 3) & 3
    layer = (b[i+1] >> 1) & 3
    bri = (b[i+2] >> 4) & 0xF
    sri = (b[i+2] >> 2) & 3
    pad = (b[i+2] >> 1) & 1
    if ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:
        return 0
    br = (_BR_V1 if ver == 3 else _BR_V2)[bri] * 1000
    sr = _SR[ver][sri]
    if br == 0 or sr == 0:
        return 0
    return (144*br//sr + pad) if ver == 3 else (72*br//sr + pad)

def strip_info_frame(b):
    """If the first frame is an Info/Xing header frame (silent, no audio), drop it."""
    flen = frame_len(b, 0)
    if flen == 0 or flen > len(b):
        return b, False
    ver = (b[1] >> 3) & 3
    mono = ((b[3] >> 6) & 3) == 3
    side = (17 if mono else 32) if ver == 3 else (9 if mono else 17)
    if b[4+side:4+side+4] in (b'Xing', b'Info'):
        return b[flen:], True
    return b, False

def frames_end(b):
    """Offset just past the last complete Layer III frame (trims trailing junk)."""
    i = 0; n = len(b)
    while i+4 <= n and not (b[i] == 0xFF and (b[i+1] & 0xE0) == 0xE0):
        i += 1
    last = i
    while i+4 <= n:
        fl = frame_len(b, i)
        if fl == 0:
            i += 1; continue
        if i+fl > n:
            break
        i += fl; last = i
    return last

def detect_le(d):
    le = struct.unpack('<Q', d[0:8])[0]
    return le < len(d)

class Sound:
    __slots__=('idx','hash','data_off','size','nsamp','rate','codec','dabs','info_off')
    def __init__(self,**k): [setattr(self,n,k[n]) for n in k]

def parse_bank(d):
    le = detect_le(d); E='<' if le else '>'
    table=struct.unpack(E+'Q',d[0:8])[0]
    total=struct.unpack(E+'I',d[0x10:0x14])[0]
    base =struct.unpack(E+'I',d[0x18:0x1c])[0]
    sio=table+total*0x10
    snds=[]
    for i in range(total):
        off=struct.unpack(E+'Q',d[table+i*0x10:table+i*0x10+8])[0]
        info=sio+off
        data_off=struct.unpack(E+'Q',d[info:info+8])[0]
        h   =struct.unpack(E+'I',d[info+0x08:info+0x0c])[0]
        size=struct.unpack(E+'I',d[info+0x0c:info+0x10])[0]
        ns  =struct.unpack(E+'I',d[info+0x10:info+0x14])[0]
        rate=struct.unpack(E+'H',d[info+0x18:info+0x1a])[0]
        cod =struct.unpack(E+'I',d[info+0x1c:info+0x20])[0]
        snds.append(Sound(idx=i,hash=h,data_off=data_off,size=size,nsamp=ns,
                          rate=rate,codec=cod,dabs=base+data_off,info_off=info))
    return dict(le=le,table=table,total=total,base=base,sio=sio,sounds=snds)

def run_mp3packer(exe,inp,outp,kbps):
    r=subprocess.run([exe,"-b",str(kbps),"-r","-f",inp,outp],capture_output=True,text=True)
    if r.returncode!=0 or not os.path.exists(outp):
        raise RuntimeError("mp3packer failed (%s):\n%s\n%s"%(inp,r.stdout,r.stderr))
    return open(outp,'rb').read()

def _new_stats(**extra):
    s = dict(swapped=0, skipped_codec=0, skipped_nomatch=0, skipped_rate=0,
             alias=0, failed=0, padded=0, truncated=0, audio_cut=0, max_cut_ms=0.0)
    s.update(extra); return s

# ---------------------------------------------------------------------------
#  DEFAULT: slot mode (v1) - write into the fixed PC slot, size/nsamp untouched
# ---------------------------------------------------------------------------
def _swap_bank_slot(pc_bytes, ps3_bytes, exe, tmp, dry, log):
    pc = parse_bank(pc_bytes)
    ps3_by_hash = {}
    for s in parse_bank(ps3_bytes)['sounds']:
        ps3_by_hash.setdefault(s.hash, s)
    out = bytearray(pc_bytes)
    done = set()
    stats = _new_stats()
    for s in pc['sounds']:
        if s.codec != 0x400:
            stats['skipped_codec'] += 1; continue
        if s.dabs in done:
            stats['alias'] += 1; continue
        src = ps3_by_hash.get(s.hash)
        if src is None:
            stats['skipped_nomatch'] += 1
            log("  [no-match] i=%d hash=%#010x"%(s.idx, s.hash)); continue
        kbps = rate_to_kbps(s.rate)
        if kbps is None:
            stats['skipped_rate'] += 1
            log("  [skip] i=%d rate=%d (no CBR match)"%(s.idx, s.rate)); continue
        ip = os.path.join(tmp, "s_%08x_in.mp3"%s.hash)
        op = os.path.join(tmp, "s_%08x_cbr.mp3"%s.hash)
        open(ip,'wb').write(ps3_bytes[src.dabs:src.dabs+src.size])
        try:
            packed = run_mp3packer(exe, ip, op, kbps)
        except Exception as e:
            stats['failed'] += 1; log("  [FAIL] i=%d: %s"%(s.idx, e)); continue
        # v1: write packed straight into the slot, no strip/trim. size/nsamp untouched.
        warn = ""
        if src.nsamp > 2*s.size:          # PS3 audio genuinely longer than the PC slot
            cut_ms = (src.nsamp - 2*s.size) * 1000.0 / s.rate if s.rate else 0.0
            stats['audio_cut'] += 1; stats['max_cut_ms'] = max(stats['max_cut_ms'], cut_ms)
            warn = "  WARNING: PS3 audio %d > slot %d samples -> ~%.0fms real tail cut" \
                   % (src.nsamp, 2*s.size, cut_ms)
        w = packed[:s.size]
        if len(packed) > s.size:
            stats['truncated'] += 1
        elif len(packed) < s.size:
            stats['padded'] += 1
        if not dry:
            out[s.dabs:s.dabs+len(w)] = w
            if len(w) < s.size:
                out[s.dabs+len(w):s.dabs+s.size] = b'\x00'*(s.size-len(w))
        done.add(s.dabs); stats['swapped'] += 1
        log("  [swap] i=%d hash=%#010x rate=%d CBR%d  PS3 %dB -> MP3 %dB / slot %dB  %s%s"
            % (s.idx, s.hash, s.rate, kbps, src.size, len(packed), s.size,
               "trunc %dB"%(len(packed)-s.size) if len(packed)>s.size
               else "pad %dB"%(s.size-len(packed)), warn))
    return bytes(out), stats

# ---------------------------------------------------------------------------
#  --grow (experimental): rebuild data region to fit the whole MP3
# ---------------------------------------------------------------------------
def _pow2_align(values, cap=0x800, floor=0x10):
    g = 0
    for v in values:
        g = math.gcd(g, v)
    if g == 0:
        return floor
    a = g & (-g)
    return min(max(a, floor), cap)

def _swap_bank_grow(pc_bytes, ps3_bytes, exe, tmp, dry, keep_info, match_length, log):
    pc = parse_bank(pc_bytes)
    if not pc['le']:
        raise RuntimeError("PC bank is not little-endian - unexpected")
    base = pc['base']
    ps3_by_hash = {}
    for s in parse_bank(ps3_bytes)['sounds']:
        ps3_by_hash.setdefault(s.hash, s)
    align = _pow2_align([base] + [s.data_off for s in pc['sounds']])
    regions, order = {}, []
    for s in pc['sounds']:
        if s.data_off not in regions:
            regions[s.data_off] = []; order.append(s.data_off)
        regions[s.data_off].append(s)
    stats = _new_stats(grew_by=0, max_overflow=0, info_stripped=0, align=align)
    plan = {}
    for off in order:
        rep = regions[off][0]
        if len(regions[off]) > 1:
            stats['alias'] += len(regions[off]) - 1
        if rep.codec != 0x400:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['skipped_codec']+=1; continue
        src = ps3_by_hash.get(rep.hash); kbps = rate_to_kbps(rep.rate)
        if src is None:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['skipped_nomatch']+=1; continue
        if kbps is None:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['skipped_rate']+=1; continue
        ip=os.path.join(tmp,"s_%08x_in.mp3"%rep.hash); op=os.path.join(tmp,"s_%08x_cbr.mp3"%rep.hash)
        open(ip,'wb').write(ps3_bytes[src.dabs:src.dabs+src.size])
        try:
            packed=run_mp3packer(exe,ip,op,kbps)
        except Exception as e:
            plan[off]=dict(data=pc_bytes[rep.dabs:rep.dabs+rep.size],size=rep.size,
                           nsamp=rep.nsamp,swapped=False); stats['failed']+=1
            log("  [FAIL] i=%d: %s"%(rep.idx,e)); continue
        stripped=False
        if not keep_info:
            packed,stripped=strip_info_frame(packed)
            if stripped: stats['info_stripped']+=1
        end=frames_end(packed)
        if end>0: packed=packed[:end]
        mp3_bytes=len(packed)
        if match_length and src.nsamp > 2*mp3_bytes:
            packed=packed+b'\x00'*(((src.nsamp+1)//2)-mp3_bytes); stats['padded']+=1
        over=len(packed)-rep.size
        if over>0: stats['max_overflow']=max(stats['max_overflow'],over); stats['grew_by']+=over
        plan[off]=dict(data=packed,size=len(packed),nsamp=2*len(packed),swapped=True)
        stats['swapped']+=1
    out=bytearray(pc_bytes[:base]); blob=bytearray()
    for off in order:
        P=plan[off]; blob+=b'\x00'*((-len(blob))%align); new_off=len(blob); blob+=P['data']
        for s in regions[off]:
            struct.pack_into('<Q',out,s.info_off+0x00,new_off)
            if P['swapped']:
                struct.pack_into('<I',out,s.info_off+0x0c,P['size'])
                struct.pack_into('<I',out,s.info_off+0x10,P['nsamp'])
    blob+=b'\x00'*((-len(blob))%align); out+=blob
    return bytes(out), stats

def swap_bank(pc_bytes, ps3_bytes, mp3packer_exe, tmpdir=None, dry=False,
              keep_info=False, grow=True, match_length=False, log=print):
    """Default = grow mode (rebuild data region, fit the whole MP3). grow=False
    selects slot mode (v1: write into the fixed PC slot, truncate the tail)."""
    tmp = tmpdir or tempfile.mkdtemp(prefix="bankswap_")
    os.makedirs(tmp, exist_ok=True)
    if grow:
        return _swap_bank_grow(pc_bytes, ps3_bytes, mp3packer_exe, tmp, dry,
                               keep_info, match_length, log)
    return _swap_bank_slot(pc_bytes, ps3_bytes, mp3packer_exe, tmp, dry, log)

def verify_bank(bank_bytes, log=print):
    b = parse_bank(bank_bytes); ok = True
    for s in b['sounds']:
        end = s.dabs + s.size
        if s.dabs < b['base'] or end > len(bank_bytes):
            log("  [VERIFY-FAIL] i=%d data %#x..%#x out of bounds"%(s.idx,s.dabs,end)); ok=False
    log("  [verify] %s (%d sounds, %d B)"%("OK" if ok else "FAILED",len(b['sounds']),len(bank_bytes)))
    return ok

def main():
    ap=argparse.ArgumentParser(description="GTA IV bank payload swap (PC ADPCM <- PS3 MP3)")
    ap.add_argument("pc_bank"); ap.add_argument("ps3_bank")
    ap.add_argument("-o","--out",required=True)
    ap.add_argument("--mp3packer",default="mp3packer")
    ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--slot",action="store_true",
                    help="slot mode (v1): write into the fixed PC slot, truncate the tail")
    ap.add_argument("--match-length",action="store_true",help="(grow only) pad to PS3 length")
    ap.add_argument("--keep-info",action="store_true",help="(grow only) keep leading Info/Xing frame")
    a=ap.parse_args()
    pc=open(a.pc_bank,'rb').read(); ps3=open(a.ps3_bank,'rb').read()
    out,st=swap_bank(pc,ps3,a.mp3packer,dry=a.dry_run,grow=not a.slot,
                     match_length=a.match_length,keep_info=a.keep_info)
    print("stats:",st)
    if st['audio_cut']:
        print("NOTE: %d sound(s) had PS3 audio longer than the PC slot -> real tail cut "
              "(max ~%.0fms). These are the exceptions; everything else only lost silence."
              % (st['audio_cut'], st['max_cut_ms']))
    if not a.dry_run:
        verify_bank(out)
        open(a.out,'wb').write(out); print("written:",a.out)

if __name__=="__main__":
    main()

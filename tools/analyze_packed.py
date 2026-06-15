#!/usr/bin/env python3
# Analyzes the STRUCTURE of a (packed) MP3: bitrate histogram, bytes between
# frames (= stuffing/signature), false-sync suspicion. Clarifies whether
# mp3packer produced true uniform CBR-128 (all frames 576 B, nothing in between)
# or "minimal frames + stuffing".
import sys

BR_MPEG1_L3 = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]  # kbps, idx 1..14
SR = {0:44100,1:48000,2:32000}

def parse(path):
    d = open(path,'rb').read()
    n = len(d); i = 0; frames = []; gap = 0; gaps = []
    # skip leading non-frame bytes (ID3 etc.) until the first sync
    while i < n-4:
        if d[i]==0xFF and (d[i+1]&0xE0)==0xE0:
            ver=(d[i+1]>>3)&3; layer=(d[i+1]>>1)&3
            bri=(d[i+2]>>4)&0xF; sri=(d[i+2]>>2)&3; pad=(d[i+2]>>1)&1
            if ver==3 and layer==1 and bri not in (0,15) and sri!=3:  # valid MPEG1 Layer III
                br=BR_MPEG1_L3[bri]; sr=SR[sri]
                size=(144*br*1000)//sr + pad
                if size>0 and i+size<=n:
                    frames.append((i,br,size,pad)); 
                    if gap: gaps.append(gap); gap=0
                    i+=size; continue
        i+=1; gap+=1
    return d, frames, gaps

def main():
    if len(sys.argv)<2:
        print("usage: py analyze_packed.py <file.mp3> [file2.mp3 ...]"); return
    for path in sys.argv[1:]:
        d,frames,gaps = parse(path)
        print("="*60); print(path, " size=", len(d))
        if not frames: print("  NO valid MPEG1 L3 frames found!"); continue
        hist={}; 
        for (_,br,_,_) in frames: hist[br]=hist.get(br,0)+1
        totjunk=sum(gaps)
        print("  total frames:", len(frames))
        print("  bitrate histogram (kbps -> frames):")
        for br in sorted(hist): print("     %4d -> %d" % (br, hist[br]))
        print("  frames with padding bit:", sum(1 for (_,_,_,p) in frames if p))
        print("  gaps between frames: %d count, %d bytes total (%.2f%% of file)"
              % (len(gaps), totjunk, 100.0*totjunk/max(1,len(d))))
        if gaps:
            import statistics
            print("     gap sizes: min=%d max=%d median=%d" % (min(gaps),max(gaps),int(statistics.median(gaps))))
        # diagnosis
        only128 = (set(hist)=={128})
        print("  --> %s" % ("TRUE uniform CBR-128, no gaps (clean for minimp3)"
                             if only128 and totjunk==0 else
                             "NOT clean: mixed bitrates and/or bytes between frames"))
        # show the first few gap bytes (signature?)
        # (informational only: show bytes right after the 10th frame if there's a gap)
    print("="*60)

if __name__=="__main__": main()

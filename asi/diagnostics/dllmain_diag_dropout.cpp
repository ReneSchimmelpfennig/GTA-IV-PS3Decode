// ============================================================================
//  GTA IV - MP3 decoder  -  DIAG-DROPOUT  (= Fix #5 + sparse event log)
//  Behavior identical to Fix #5. Logs only events that could explain a short
//  dropout, with a call counter (so the period is readable):
//    [vID] INIT-RESET call=N          (voice reinit -> context cleared)
//    [vID] RESYNC     call=N          (minimp3 frame_bytes==0, 1-byte resync)
//    [vID] UNDERRUN   call=N real=R want=W   (decoded too little -> silence fill)
//    [vID] LATCH      call=N foff=F
//  Heartbeat every 64 calls: [vID] ok call=N
//  Steady call = 16384 bytes = 0.512 s. Period = call distance x0.512 = seconds.
// ============================================================================
#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <map>
#include <vector>
#define MINIMP3_IMPLEMENTATION
#define MINIMP3_NO_STDIO
#include "minimp3.h"
#include "MinHook.h"

static const uintptr_t RVA_Decode = 0x00497440;
static const uintptr_t RVA_Init   = 0x0048F0D0;
static const uintptr_t STATE_OFF  = 0x14E;

typedef int(__fastcall* Decode_t)(void*, void*, int16_t*, void*, unsigned int, int);
static Decode_t oDecode = nullptr;
typedef void(__fastcall* Init_t)(void*, void*, void*);
static Init_t oInit = nullptr;

struct VC {
    mp3dec_t mp3; std::vector<uint8_t> inbuf; size_t inpos;
    int16_t overflow[MINIMP3_MAX_SAMPLES_PER_FRAME]; int overflowCount; int isMp3;
    int id; int call;
};
static std::map<void*, VC*> g_ctx;
static CRITICAL_SECTION g_cs;
static FILE* g_log = nullptr;
static int g_nextId = 1;

static void LG(const char* f, ...) { if(!g_log) return; va_list a; va_start(a,f); vfprintf(g_log,f,a); va_end(a); fputc('\n',g_log); fflush(g_log); }
static VC* GetCtx(void* s){ EnterCriticalSection(&g_cs); VC* c; auto it=g_ctx.find(s);
    if(it!=g_ctx.end()) c=it->second; else { c=new VC(); mp3dec_init(&c->mp3); c->inpos=0; c->overflowCount=0; c->isMp3=-1; c->id=g_nextId++; c->call=0; g_ctx[s]=c; }
    LeaveCriticalSection(&g_cs); return c; }
static bool SC(void* d, const void* s, size_t n){ __try{ memcpy(d,s,n); return true;} __except(EXCEPTION_EXECUTE_HANDLER){ return false;} }

// returns {written, resyncs}
static int DFS(VC* c, int16_t* out, int want, int* resyncs, int* junk, int* fsz, char* jhex) {
    int written=0; *resyncs=0; *junk=0; *fsz=0; jhex[0]=0; bool jcap=false;
    if(c->overflowCount>0){ int t=(c->overflowCount<want)?c->overflowCount:want; memcpy(out,c->overflow,t*2); written+=t;
        if(t<c->overflowCount) memmove(c->overflow,c->overflow+t,(c->overflowCount-t)*2); c->overflowCount-=t; }
    mp3dec_frame_info_t info; int16_t fp[MINIMP3_MAX_SAMPLES_PER_FRAME]; const size_t MAXF=1600;
    while(written<want){ size_t avail=(c->inpos<=c->inbuf.size())?(c->inbuf.size()-c->inpos):0; if(avail<4) break;
        int s=mp3dec_decode_frame(&c->mp3,c->inbuf.data()+c->inpos,(int)avail,fp,&info);
        if(info.frame_bytes==0){ if(avail>MAXF){ c->inpos+=1; (*resyncs)++; continue;} break; }
        if(info.frame_offset>0 && !jcap){ static const char* H="0123456789abcdef"; int nn=info.frame_offset<12?info.frame_offset:12; char* p=jhex; for(int i=0;i<nn && c->inpos+(size_t)i<c->inbuf.size(); i++){ uint8_t b=c->inbuf[c->inpos+i]; *p++=H[b>>4]; *p++=H[b&0xF]; } *p=0; jcap=true; }
        *junk += info.frame_offset; *fsz = info.frame_bytes - info.frame_offset;
        c->inpos+=info.frame_bytes; if(s<=0) continue;
        int need=want-written, take=(s<need)?s:need;
        if(info.channels==1) memcpy(out+written,fp,take*2); else for(int i=0;i<take;i++) out[written+i]=fp[i*info.channels];
        written+=take;
        if(take<s){ int r=s-take; if(info.channels==1) memcpy(c->overflow,fp+take,r*2); else for(int i=0;i<r;i++) c->overflow[i]=fp[(take+i)*info.channels]; c->overflowCount=r; } }
    if(written<want) memset(out+written,0,(want-written)*2);
    if(c->inpos>(1u<<20)){ c->inbuf.erase(c->inbuf.begin(),c->inbuf.begin()+c->inpos); c->inpos=0; }
    return written;
}

int __fastcall hkDecode(void* st, void* edx, int16_t* outPcm, void* inData, unsigned int nib, int numSamples) {
    if(numSamples<=0||!outPcm||!inData) return oDecode(st,edx,outPcm,inData,nib,numSamples);
    VC* c=GetCtx(st); c->call++;
    if(c->isMp3==-1){
        std::vector<uint8_t> tmp((size_t)numSamples);
        if(!SC(tmp.data(),inData,(size_t)numSamples)) return oDecode(st,edx,outPcm,inData,nib,numSamples);
        bool z=true; for(size_t i=0;i<tmp.size();++i) if(tmp[i]){z=false;break;}
        if(z) return oDecode(st,edx,outPcm,inData,nib,numSamples);
        mp3dec_frame_info_t pi; int16_t pp[MINIMP3_MAX_SAMPLES_PER_FRAME];
        int ps=mp3dec_decode_frame(&c->mp3,tmp.data(),(int)tmp.size(),pp,&pi);
        if(ps>0&&pi.frame_bytes>0&&pi.hz>=8000){
            c->isMp3=1; mp3dec_init(&c->mp3); int off=pi.frame_offset; if(off<0||off>(int)tmp.size()) off=0;
            c->inbuf.assign(tmp.begin()+off,tmp.end()); c->inpos=0; c->overflowCount=0;
            LG("[v%d] LATCH call=%d foff=%d", c->id, c->call, pi.frame_offset);
            char jh[32]; int rs,jk,fs; int real=DFS(c,outPcm,numSamples*2,&rs,&jk,&fs,jh);
            if(rs) LG("[v%d] RESYNC call=%d n=%d", c->id, c->call, rs);
            if(real<numSamples*2) LG("[v%d] UNDERRUN call=%d real=%d want=%d junk=%d fsz=%d jhex=%s", c->id, c->call, real, numSamples*2, jk, fs, jh);
            return numSamples*4;
        }
        c->isMp3=0;
    }
    if(c->isMp3!=1) return oDecode(st,edx,outPcm,inData,nib,numSamples);
    size_t old=c->inbuf.size(); c->inbuf.resize(old+(size_t)numSamples);
    if(!SC(c->inbuf.data()+old,inData,(size_t)numSamples)) c->inbuf.resize(old);
    char jh[32]; int rs,jk,fs; int real=DFS(c,outPcm,numSamples*2,&rs,&jk,&fs,jh);
    if(rs) LG("[v%d] RESYNC call=%d n=%d", c->id, c->call, rs);
    if(real<numSamples*2) LG("[v%d] UNDERRUN call=%d real=%d want=%d junk=%d fsz=%d jhex=%s", c->id, c->call, real, numSamples*2, jk, fs, jh);
    else if((c->call & 63)==0) LG("[v%d] ok call=%d junk=%d fsz=%d", c->id, c->call, jk, fs);
    return numSamples*4;
}

void __fastcall hkInit(void* voice, void* edx, void* p2) {
    void* st=(void*)((char*)voice+STATE_OFF);
    EnterCriticalSection(&g_cs); auto it=g_ctx.find(st);
    if(it!=g_ctx.end()){ LG("[v%d] INIT-RESET call=%d", it->second->id, it->second->call); delete it->second; g_ctx.erase(it); }
    LeaveCriticalSection(&g_cs);
    oInit(voice,edx,p2);
}

static DWORD WINAPI Inst(LPVOID){ Sleep(2000); InitializeCriticalSection(&g_cs);
    fopen_s(&g_log,"gta4mp3.log","a"); LG("=== DIAG-DROPOUT-Build ===");
    uintptr_t b=(uintptr_t)GetModuleHandleA(nullptr); void* t=(void*)(b+RVA_Decode);
    if(MH_Initialize()!=MH_OK) return 1;
    if(MH_CreateHook(t,&hkDecode,(void**)&oDecode)!=MH_OK) return 1; if(MH_EnableHook(t)!=MH_OK) return 1;
    void* ti=(void*)(b+RVA_Init); if(MH_CreateHook(ti,&hkInit,(void**)&oInit)==MH_OK) MH_EnableHook(ti);
    return 0; }
BOOL APIENTRY DllMain(HMODULE h, DWORD r, LPVOID){ if(r==DLL_PROCESS_ATTACH){ DisableThreadLibraryCalls(h); CreateThread(0,0,Inst,0,0,0);} else if(r==DLL_PROCESS_DETACH){ if(g_log) fclose(g_log); MH_Uninitialize(); } return TRUE; }

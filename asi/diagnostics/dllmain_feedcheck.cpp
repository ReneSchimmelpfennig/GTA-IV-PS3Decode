// ============================================================================
//  GTA IV - FEED-CHECK  : verifies that the DecodeDriver delivers contiguous
//  MP3 bytes. Decode = Fix #5 (audio plays), plus a log PER CALL: voice id,
//  call#, cumulative input offset BEFORE this call, numSamples (=bytes), and
//  the first 12 bytes of inData.
//  -> can be compared offline against ch0_r.mp3: must run contiguously.
// ============================================================================
#include <windows.h>
#include <cstdint>
#include <vector>
#include <map>
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

struct VC { mp3dec_t mp3; std::vector<uint8_t> inbuf; size_t inpos; int isMp3; int id; int call; uint64_t cumIn; };
static std::map<void*, VC*> g_ctx;
static CRITICAL_SECTION g_cs; static FILE* g_log=nullptr; static int g_nextId=1;
static const char* HX="0123456789abcdef";

static void LGhex(VC* c,int ns,const uint8_t* p,bool ok){
    if(!g_log) return; char h[40]; int nn=ns<12?ns:12; char* q=h;
    for(int i=0;i<nn;i++){ *q++=HX[p[i]>>4]; *q++=HX[p[i]&0xF]; } *q=0;
    fprintf(g_log,"[v%d] call=%d cumIn=%llu ns=%d in=%s%s\n",
            c->id,c->call,(unsigned long long)c->cumIn,ns,h, ok?"":" (copyfail)");
    fflush(g_log);
}
static VC* GetCtx(void* s){ EnterCriticalSection(&g_cs); VC* c; auto it=g_ctx.find(s);
    if(it!=g_ctx.end()) c=it->second; else { c=new VC(); mp3dec_init(&c->mp3); c->inpos=0; c->isMp3=-1; c->id=g_nextId++; c->call=0; c->cumIn=0; g_ctx[s]=c; }
    LeaveCriticalSection(&g_cs); return c; }
static bool SC(void* d,const void* s,size_t n){ __try{ memcpy(d,s,n); return true;} __except(EXCEPTION_EXECUTE_HANDLER){ return false;} }

static int DFS(VC* c,int16_t* out,int want){
    int written=0; mp3dec_frame_info_t info; int16_t fp[MINIMP3_MAX_SAMPLES_PER_FRAME]; const size_t MAXF=1600;
    while(written<want){ size_t avail=(c->inpos<=c->inbuf.size())?(c->inbuf.size()-c->inpos):0; if(avail<4) break;
        int s=mp3dec_decode_frame(&c->mp3,c->inbuf.data()+c->inpos,(int)avail,fp,&info);
        if(info.frame_bytes==0){ if(avail>MAXF){c->inpos++;continue;} break; }
        c->inpos+=info.frame_bytes; if(s<=0) continue;
        int need=want-written,take=(s<need)?s:need;
        if(info.channels==1) memcpy(out+written,fp,take*2); else for(int i=0;i<take;i++) out[written+i]=fp[i*info.channels];
        written+=take; }
    if(written<want) memset(out+written,0,(want-written)*2);
    if(c->inpos>(1u<<20)){ c->inbuf.erase(c->inbuf.begin(),c->inbuf.begin()+c->inpos); c->inpos=0; }
    return written;
}

int __fastcall hkDecode(void* st,void* edx,int16_t* outPcm,void* inData,unsigned int nib,int numSamples){
    if(numSamples<=0||!outPcm||!inData) return oDecode(st,edx,outPcm,inData,nib,numSamples);
    VC* c=GetCtx(st); c->call++;
    uint8_t head[12]={0}; bool ok=SC(head,inData,(size_t)(numSamples<12?numSamples:12));
    if(c->isMp3==-1){
        std::vector<uint8_t> tmp((size_t)numSamples);
        if(!SC(tmp.data(),inData,(size_t)numSamples)) return oDecode(st,edx,outPcm,inData,nib,numSamples);
        bool z=true; for(size_t i=0;i<tmp.size();++i) if(tmp[i]){z=false;break;}
        if(z) return oDecode(st,edx,outPcm,inData,nib,numSamples);   // silence: do not count
        mp3dec_frame_info_t pi; int16_t pp[MINIMP3_MAX_SAMPLES_PER_FRAME];
        int ps=mp3dec_decode_frame(&c->mp3,tmp.data(),(int)tmp.size(),pp,&pi);
        if(ps>0&&pi.frame_bytes>0&&pi.hz>=8000){
            LGhex(c,numSamples,head,ok); c->cumIn+=numSamples;
            c->isMp3=1; mp3dec_init(&c->mp3); int off=pi.frame_offset; if(off<0||off>(int)tmp.size()) off=0;
            c->inbuf.assign(tmp.begin()+off,tmp.end()); c->inpos=0;
            DFS(c,outPcm,numSamples*2); return numSamples*4;
        }
        c->isMp3=0;
    }
    if(c->isMp3!=1) return oDecode(st,edx,outPcm,inData,nib,numSamples);
    LGhex(c,numSamples,head,ok); c->cumIn+=numSamples;
    size_t old=c->inbuf.size(); c->inbuf.resize(old+(size_t)numSamples);
    if(!SC(c->inbuf.data()+old,inData,(size_t)numSamples)) c->inbuf.resize(old);
    DFS(c,outPcm,numSamples*2); return numSamples*4;
}
void __fastcall hkInit(void* voice,void* edx,void* p2){ void* st=(void*)((char*)voice+STATE_OFF);
    EnterCriticalSection(&g_cs); auto it=g_ctx.find(st); if(it!=g_ctx.end()){ if(g_log) fprintf(g_log,"[v%d] INIT-RESET call=%d cumIn=%llu\n",it->second->id,it->second->call,(unsigned long long)it->second->cumIn); delete it->second; g_ctx.erase(it);} LeaveCriticalSection(&g_cs);
    oInit(voice,edx,p2); }
static DWORD WINAPI Inst(LPVOID){ Sleep(2000); InitializeCriticalSection(&g_cs); fopen_s(&g_log,"gta4mp3.log","a");
    if(g_log) fprintf(g_log,"=== FEED-CHECK ===\n");
    uintptr_t b=(uintptr_t)GetModuleHandleA(nullptr); if(MH_Initialize()!=MH_OK) return 1;
    void* t=(void*)(b+RVA_Decode); if(MH_CreateHook(t,&hkDecode,(void**)&oDecode)!=MH_OK) return 1; if(MH_EnableHook(t)!=MH_OK) return 1;
    void* ti=(void*)(b+RVA_Init); if(MH_CreateHook(ti,&hkInit,(void**)&oInit)==MH_OK) MH_EnableHook(ti); return 0; }
BOOL APIENTRY DllMain(HMODULE h,DWORD r,LPVOID){ if(r==DLL_PROCESS_ATTACH){DisableThreadLibraryCalls(h);CreateThread(0,0,Inst,0,0,0);} else if(r==DLL_PROCESS_DETACH){ if(g_log) fclose(g_log); MH_Uninitialize(); } return TRUE; }

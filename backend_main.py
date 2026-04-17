"""
SocialScope — Backend API v2
What's new:
  - YouTube: reads defaultAudioLanguage, falls back to transcript + langdetect
  - TikTok:  downloads first 10s audio via yt-dlp, detects language with faster-whisper
  - Language filter applied after scraping based on actual spoken language
  - YouTube API now passes regionCode + relevanceLanguage

New packages (run once):
    pip install faster-whisper yt-dlp langdetect youtube-transcript-api

Run:
    uvicorn backend_main_v2:app --reload --port 8000
"""

import os, asyncio, re, uuid, tempfile, sys, json
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
YOUTUBE_API_KEY   = os.getenv("YOUTUBE_API_KEY", "")
TIKTOK_MS_TOKEN   = os.getenv("TIKTOK_MS_TOKEN", "")
TIKTOK_PROXY      = os.getenv("TIKTOK_PROXY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_whisper_model    = None
jobs: dict[str, dict] = {}

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        print("[Whisper] tiny model loaded.")
    return _whisper_model

LANG_NAMES = {
    "id":"Indonesian","en":"English","ja":"Japanese","ko":"Korean",
    "th":"Thai","vi":"Vietnamese","ms":"Malay","zh":"Chinese",
    "pt":"Portuguese","es":"Spanish","fr":"French","ar":"Arabic",
    "hi":"Hindi","tl":"Filipino","unknown":"Unknown",
}

# ── Collaboration detection ───────────────────────────────────────────────
_C_HIGH_TAGS = re.compile(
    r'#(ad|sponsored|partnership|paidpartnership|collaboration|collab|gifted|promo)\b',
    re.IGNORECASE,
)
_C_AFF_URLS = re.compile(
    r'(onelink\.to|bit\.ly|linktr\.ee|go\.link|app\.link'
    r'|adjust\.com|branch\.io|kochava\.com|appsflyer\.com)',
    re.IGNORECASE,
)
_C_UTM = re.compile(r'[?&](utm_\w+|ref=|aff=)', re.IGNORECASE)
_C_MED_WORDS = re.compile(
    r'\b(sponsored|paid promotion|in partnership with|in collaboration with'
    r'|ambassador|official partner|presented by|supported by)\b',
    re.IGNORECASE,
)
_C_PROMO_CODE = re.compile(
    r'\b(use code|promo code|discount code|coupon code)\s+[A-Z0-9]{3,20}\b',
    re.IGNORECASE,
)
_C_AT_BRAND   = re.compile(r'@[A-Za-z][A-Za-z0-9_.]{2,}')
_C_LINK_IN_BIO = re.compile(r'\blink[- ]?in[- ]?bio\b', re.IGNORECASE)

def detect_collaboration(video: dict) -> dict:
    """Analyse a video dict and return collaboration metadata."""
    full = f"{video.get('title','')} {video.get('caption','')}"
    signals: list[str] = []

    # HIGH — any one match is conclusive
    m = _C_HIGH_TAGS.search(full)
    if m:
        signals.append(f"#{m.group(1).lower()} in caption")
    m = _C_AFF_URLS.search(full)
    if m:
        signals.append(f"{m.group(1)} URL found")
    if _C_UTM.search(full):
        signals.append("tracking URL parameters found")
    if signals:
        return {"is_collab": True, "confidence": "high", "signals": signals}

    # MEDIUM
    m = _C_MED_WORDS.search(full)
    if m:
        signals.append(f'"{m.group(0).lower()}" in caption')
    if _C_PROMO_CODE.search(full):
        signals.append("discount/promo code detected")
    if signals:
        return {"is_collab": True, "confidence": "medium", "signals": signals}

    # LOW
    brands = _C_AT_BRAND.findall(full)
    if brands:
        signals.append(f"@mention in caption: {brands[0]}")
    if _C_LINK_IN_BIO.search(full) and brands:
        signals.append("link in bio with brand @mention")
    if signals:
        return {"is_collab": True, "confidence": "low", "signals": signals}

    return {"is_collab": False, "confidence": "low", "signals": []}

class SearchRequest(BaseModel):
    keywords:        list[str]
    platforms:       list[str]
    region:          str       = "US"
    language:        str       = "any"
    detect_language: bool      = True
    exact_mode:      bool      = False
    strict_language: bool      = False
    count:           int       = 30
    min_views:       int       = 0
    min_followers:   int       = 0
    days_back:       int       = 30
    search_variants: list[str] = []

class AccountRequest(BaseModel):
    handle: str
    platforms: list[str]

class LookupRequest(BaseModel):
    keyword: str

FLAG_MAP = {
    "ja":"🇯🇵","ko":"🇰🇷","zh":"🇨🇳","id":"🇮🇩","th":"🇹🇭",
    "vi":"🇻🇳","ar":"🇸🇦","hi":"🇮🇳","en":"🌐","ms":"🇲🇾",
}

def _v(code, language, name):
    return {"code": code, "language": language, "name": name, "flag": FLAG_MAP.get(code, "🌐")}

FALLBACK_VARIANTS: dict[str, list[dict]] = {
    "haikyu": [
        _v("ja","Japanese","ハイキュー!!"), _v("ko","Korean","하이큐!!"),
        _v("zh","Chinese","排球少年"),       _v("id","Indonesian","Haikyuu"),
        _v("en","English","Haikyuu!!"),
    ],
    "haikyuu": [
        _v("ja","Japanese","ハイキュー!!"), _v("ko","Korean","하이큐!!"),
        _v("zh","Chinese","排球少年"),       _v("en","English","Haikyu!!"),
    ],
    "naruto": [
        _v("ja","Japanese","ナルト"),   _v("ko","Korean","나루토"),
        _v("zh","Chinese","火影忍者"),  _v("ar","Arabic","ناروتو"),
        _v("hi","Hindi","नारुतो"),
    ],
    "one piece": [
        _v("ja","Japanese","ワンピース"), _v("ko","Korean","원피스"),
        _v("zh","Chinese","海賊王"),      _v("th","Thai","วันพีซ"),
    ],
    "attack on titan": [
        _v("ja","Japanese","進撃の巨人"),  _v("ko","Korean","진격의 거인"),
        _v("zh","Chinese","进击的巨人"),   _v("id","Indonesian","Shingeki no Kyojin"),
        _v("ar","Arabic","هجوم العمالقة"),
    ],
    "demon slayer": [
        _v("ja","Japanese","鬼滅の刃"),  _v("ko","Korean","귀멸의 칼날"),
        _v("zh","Chinese","鬼灭之刃"),   _v("id","Indonesian","Kimetsu no Yaiba"),
        _v("th","Thai","ดาบพิฆาตอสูร"),
    ],
    "genshin impact": [
        _v("ja","Japanese","原神"), _v("ko","Korean","원신"),
        _v("zh","Chinese","原神"),  _v("th","Thai","เกนชินอิมแพกต์"),
    ],
    "blue lock": [
        _v("ja","Japanese","ブルーロック"), _v("ko","Korean","블루 록"),
        _v("zh","Chinese","蓝色监狱"),
    ],
    "jujutsu kaisen": [
        _v("ja","Japanese","呪術廻戦"),  _v("ko","Korean","주술회전"),
        _v("zh","Chinese","咒术回战"),   _v("th","Thai","มหาเวทย์ผนึกมาร"),
    ],
    "valorant": [
        _v("ja","Japanese","ヴァロラント"), _v("ko","Korean","발로란트"),
        _v("zh","Chinese","无畏契约"),      _v("th","Thai","วาโลแรนต์"),
    ],
    "league of legends": [
        _v("ja","Japanese","リーグ・オブ・レジェンド"), _v("ko","Korean","리그 오브 레전드"),
        _v("zh","Chinese","英雄联盟"),                  _v("th","Thai","ลีกออฟเลเจนส์"),
    ],
    "mobile legends": [
        _v("id","Indonesian","Mobile Legends: Bang Bang"),
        _v("th","Thai","โมบายเลเจนส์"),
        _v("vi","Vietnamese","Mobile Legends: Bang Bang"),
        _v("ms","Malay","Mobile Legends: Bang Bang"),
    ],
    "free fire": [
        _v("id","Indonesian","Garena Free Fire"), _v("th","Thai","การีนา ฟรีไฟร์"),
        _v("vi","Vietnamese","Garena Free Fire"),  _v("ar","Arabic","فري فاير"),
    ],
}

def fmt(n):
    if n>=1_000_000: return f"{n/1_000_000:.1f}M"
    if n>=1_000: return f"{n/1_000:.1f}K"
    return str(n)

def ht(t): return re.findall(r"#(\w+)", t)

def er(l,c,s,v): return round((l+c+s)/v*100,2) if v else 0.0

async def detect_yt_lang(vid_id, snip):
    """Return (language_code, source) where source is 'api'|'transcript'|'title'|'unknown'."""
    from langdetect import detect, LangDetectException

    # 1. defaultAudioLanguage metadata — most authoritative
    dal = snip.get("defaultAudioLanguage")
    if dal:
        lang = dal.split("-")[0]
        print(f"  [Lang] {vid_id}: defaultAudioLanguage={dal} -> {lang}")
        return lang, "api"

    # 2. defaultLanguage metadata
    dl = snip.get("defaultLanguage")
    if dl:
        lang = dl.split("-")[0]
        print(f"  [Lang] {vid_id}: defaultLanguage={dl} -> {lang}")
        return lang, "api"

    # 3. Transcript — fetch first available regardless of language, run langdetect
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        tl  = await asyncio.to_thread(api.list, vid_id)
        transcripts = list(tl)
        print(f"  [Lang] {vid_id}: transcripts available: {[t.language_code for t in transcripts[:5]]}")
        if transcripts:
            # Prefer auto-generated (language_code reflects spoken language); fall back to manual
            auto   = [t for t in transcripts if t.is_generated]
            chosen = (auto or transcripts)[0]
            print(f"  [Lang] {vid_id}: fetching code={chosen.language_code} generated={chosen.is_generated}")
            tr   = await asyncio.to_thread(chosen.fetch)
            text = " ".join(s.text for s in list(tr)[:60])
            print(f"  [Lang] {vid_id}: transcript text len={len(text.strip())}")
            if len(text.strip()) >= 50:
                try:
                    lang = detect(text)
                    print(f"  [Lang] {vid_id}: transcript langdetect -> {lang}")
                    return lang, "transcript"
                except LangDetectException as e:
                    print(f"  [Lang] {vid_id}: transcript langdetect failed: {e}")
    except Exception as e:
        print(f"  [Lang] {vid_id}: transcript error: {type(e).__name__}: {e}")

    # 4. Title + description fallback — works for Shorts and caption-disabled videos
    try:
        title = snip.get("title", "").strip()
        desc  = snip.get("description", "").strip()
        combined = (title + " " + desc).strip()
        print(f"  [Lang] {vid_id}: title+desc len={len(combined)}")
        if len(combined) >= 20:
            try:
                lang = detect(combined)
                print(f"  [Lang] {vid_id}: title+desc langdetect -> {lang}")
                return lang, "title"
            except LangDetectException as e:
                print(f"  [Lang] {vid_id}: title+desc langdetect failed: {e}")
    except Exception as e:
        print(f"  [Lang] {vid_id}: title+desc error: {e}")

    print(f"  [Lang] {vid_id}: -> unknown")
    return "unknown", "unknown"

async def detect_tt_lang(url):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "clip.mp3")
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp","--no-playlist","--extract-audio","--audio-format","mp3",
                "--audio-quality","9","--postprocessor-args","-t 10",
                "-o",out,"--quiet",url,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=30)
            if not os.path.exists(out): return "unknown"
            m = await asyncio.to_thread(get_whisper)
            segs, info = await asyncio.to_thread(
                m.transcribe, out, beam_size=1, language=None, task="transcribe",
                without_timestamps=True, condition_on_previous_text=False)
            list(segs)
            return info.language or "unknown"
    except asyncio.TimeoutError: return "unknown"
    except Exception as e:
        print(f"  [WARN] tt lang detect: {e}"); return "unknown"

async def search_youtube(keyword, count=30, days_back=30, region="US", language="any", detect_lang=True, exact_mode=False):
    if not YOUTUBE_API_KEY:
        raise ValueError("YouTube API key not configured")
    q   = f'"{keyword}"' if exact_mode else keyword
    pub = (datetime.utcnow()-timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with httpx.AsyncClient() as c:
        yt_params = {
            "key":YOUTUBE_API_KEY,"q":q,"part":"snippet","type":"video",
            "maxResults":min(count,50),"publishedAfter":pub,"order":"viewCount"}
        if region and region != "GLOBAL":
            yt_params["regionCode"] = region
        if language and language != "any":
            yt_params["relevanceLanguage"] = language
        sr = await c.get("https://www.googleapis.com/youtube/v3/search", params=yt_params, timeout=15)
        items = sr.json().get("items",[])
        if not items: return []
        vids = [i["id"]["videoId"] for i in items if "videoId" in i.get("id",{})]
        chs  = list({i["snippet"]["channelId"] for i in items})
        vr = await c.get("https://www.googleapis.com/youtube/v3/videos",
            params={"key":YOUTUBE_API_KEY,"id":",".join(vids),"part":"statistics,contentDetails,snippet"},timeout=15)
        vm = {v["id"]:v for v in vr.json().get("items",[])}
        cr = await c.get("https://www.googleapis.com/youtube/v3/channels",
            params={"key":YOUTUBE_API_KEY,"id":",".join(chs[:50]),"part":"statistics,snippet"},timeout=15)
        cm = {ch["id"]:ch for ch in cr.json().get("items",[])}
    results=[]
    for item in items:
        vid=item["id"].get("videoId",""); snip=item["snippet"]; ch_id=snip["channelId"]
        vd=vm.get(vid,{}); st=vd.get("statistics",{}); vs=vd.get("snippet",snip)
        ch=cm.get(ch_id,{}); cs=ch.get("statistics",{}); csn=ch.get("snippet",{})
        v=int(st.get("viewCount",0)); l=int(st.get("likeCount",0))
        co=int(st.get("commentCount",0)); s=int(cs.get("subscriberCount",0))
        lang, lang_src = await detect_yt_lang(vid,vs) if detect_lang else ("unknown","unknown")
        row = {
            "platform":"youtube","id":vid,"url":f"https://www.youtube.com/watch?v={vid}",
            "thumbnail":snip.get("thumbnails",{}).get("medium",{}).get("url",""),
            "title":snip.get("title",""),"caption":snip.get("description","")[:200],
            "hashtags":ht(snip.get("description","")),"upload_date":snip.get("publishedAt","")[:10],
            "views":v,"likes":l,"comments":co,"shares":0,
            "views_fmt":fmt(v),"likes_fmt":fmt(l),"comments_fmt":fmt(co),
            "spoken_language":lang,"spoken_language_name":LANG_NAMES.get(lang,lang.upper()),
            "language_source":lang_src,
            "account":{"id":ch_id,"username":csn.get("customUrl",ch_id),
                "display_name":snip.get("channelTitle",""),"followers":s,"followers_fmt":fmt(s),
                "profile_url":f"https://www.youtube.com/channel/{ch_id}","verified":bool(csn.get("country"))},
            "engagement_rate":er(l,co,0,v),"keyword":keyword,"region":region,
        }
        row["collaboration"] = detect_collaboration(row)
        results.append(row)
    return results

async def search_tiktok(keyword, count=30, region="US", detect_lang=True, exact_mode=False):
    """Search TikTok via TikTokApi + Playwright. Run backend locally and expose via ngrok."""
    try:
        from TikTokApi import TikTokApi  # noqa: F401
    except ImportError:
        raise ImportError("TikTokApi not installed")
    try:
        return await asyncio.to_thread(_tiktok_search_sync, keyword, count, region, detect_lang, exact_mode)
    except (ImportError, ConnectionError):
        raise
    except Exception as e:
        msg = str(e) or repr(e) or type(e).__name__
        raise ConnectionError(f"TikTok unreachable: {msg}") from e

def _tiktok_search_sync(keyword, count, region, detect_lang, exact_mode=False):
    """Runs TikTok search in a fresh event loop to avoid Playwright/uvicorn conflicts on Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_tiktok_search_inner(keyword, count, region, detect_lang, exact_mode))
    finally:
        loop.close()

async def _tiktok_search_inner(keyword, count, region, detect_lang, exact_mode=False):
    from TikTokApi import TikTokApi
    search_kw = f'"{keyword}"' if exact_mode else keyword
    results = []
    async with TikTokApi() as api:
        print(f"[TikTok] Creating session…")
        try:
            await asyncio.wait_for(
                api.create_sessions(
                    ms_tokens=[TIKTOK_MS_TOKEN] if TIKTOK_MS_TOKEN else None,
                    num_sessions=1, sleep_after=3, timeout=90000,
                    proxies=[{"server": TIKTOK_PROXY}] if TIKTOK_PROXY else None,
                ),
                timeout=120
            )
        except asyncio.TimeoutError as e:
            raise ConnectionError("TikTok session failed: timed out after 120s") from e
        except Exception as e:
            msg = str(e) or repr(e) or type(e).__name__
            raise ConnectionError(f"TikTok session failed: {msg}") from e
        print(f"[TikTok] Session OK — searching '{search_kw}'")
        async for video in api.search.search_type(search_kw, "item", count=count):
            try:
                d=video.as_dict; au=video.author
                st=d.get("statsV2") or d.get("stats",{})
                ast=d.get("authorStats",{}); mu=d.get("music",{})
                au_d=d.get("author",{})
                v=int(st.get("playCount",0) or 0); l=int(st.get("diggCount",0) or 0)
                co=int(st.get("commentCount",0) or 0); sh=int(st.get("shareCount",0) or 0)
                f=int(ast.get("followerCount",0) or 0)
                desc=d.get("desc","")
                url=f"https://www.tiktok.com/@{au.username}/video/{video.id}"
                lang = await detect_tt_lang(url) if detect_lang else "unknown"
                row = {
                    "platform":"tiktok","id":str(video.id),"url":url,
                    "thumbnail":d.get("video",{}).get("cover",""),
                    "title":desc[:100],"caption":desc,"hashtags":ht(desc),
                    "upload_date":datetime.fromtimestamp(int(d.get("createTime",0))).strftime("%Y-%m-%d"),
                    "views":v,"likes":l,"comments":co,"shares":sh,
                    "views_fmt":fmt(v),"likes_fmt":fmt(l),"comments_fmt":fmt(co),
                    "spoken_language":lang,"spoken_language_name":LANG_NAMES.get(lang,lang.upper()),
                    "account":{"id":str(au.user_id),"username":f"@{au.username}",
                        "display_name":au_d.get("nickname",au.username),"followers":f,"followers_fmt":fmt(f),
                        "profile_url":f"https://www.tiktok.com/@{au.username}",
                        "verified":au_d.get("verified",False)},
                    "audio":{"title":mu.get("title",""),"artist":mu.get("authorName",""),"original":mu.get("original",False)},
                    "engagement_rate":er(l,co,sh,v),"keyword":keyword,"region":region,
                }
                row["collaboration"] = detect_collaboration(row)
                results.append(row)
            except Exception as ve:
                print(f"  [TikTok] skipping video: {ve}"); continue
    return results

async def run_search_job(job_id, req):
    jobs[job_id].update({"status":"running","progress":0,"platform_errors":{}})
    all_results = []

    # Expand each keyword with its variants so we search all of them
    search_terms = []  # (main_keyword, search_term, platform)
    for kw in req.keywords:
        for platform in req.platforms:
            search_terms.append((kw, kw, platform))
            for variant in req.search_variants:
                search_terms.append((kw, variant, platform))

    total = len(search_terms); done = 0
    for main_kw, term, platform in search_terms:
        try:
            if platform=="youtube":
                r=await search_youtube(term,req.count,req.days_back,req.region,req.language,req.detect_language,req.exact_mode)
            elif platform=="tiktok":
                r=await search_tiktok(term,req.count,req.region,req.detect_language,req.exact_mode)
            else: r=[]
            for x in r:
                x["keyword"] = main_kw
                x["search_variant"] = term
            r=[x for x in r if x["views"]>=req.min_views and x["account"]["followers"]>=req.min_followers]
            if req.language and req.language != "any":
                if req.strict_language:
                    r = [x for x in r if x.get("spoken_language") == req.language]
                else:
                    r = [x for x in r if x.get("spoken_language") in (req.language, "unknown")]
            all_results.extend(r)
        except Exception as e:
            msg = str(e)
            jobs[job_id]["platform_errors"][platform] = msg
            print(f"[Job {job_id}] {platform} error: {msg}")
        done+=1; jobs[job_id]["progress"]=int(done/total*100)
    seen,unique=set(),[]
    for r in all_results:
        k=f"{r['platform']}_{r['id']}"
        if k not in seen: seen.add(k); unique.append(r)
    lc:dict[str,int]={}; bp:dict[str,int]={}
    for r in unique:
        l=r.get("spoken_language","unknown"); lc[l]=lc.get(l,0)+1; bp[r["platform"]]=bp.get(r["platform"],0)+1
    tv=sum(r["views"] for r in unique)
    jobs[job_id].update({"status":"done","results":unique,"summary":{
        "total":len(unique),"total_views":tv,"total_views_fmt":fmt(tv),
        "avg_engagement":round(sum(r["engagement_rate"] for r in unique)/len(unique),2) if unique else 0,
        "by_platform":bp,"by_language":lc,
        "top_posts":sorted(unique,key=lambda x:x["views"],reverse=True)[:3]},
        "platform_errors":jobs[job_id].get("platform_errors",{})})

@asynccontextmanager
async def lifespan(app):
    print("[Startup] Loading Whisper model…")
    await asyncio.to_thread(get_whisper)
    print("[Startup] Ready."); yield

app = FastAPI(title="SocialScope API", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])

@app.get("/")
def root(): return {"status":"ok","service":"SocialScope API v2.0"}

@app.get("/health")
async def health():
    # YouTube: attempt a real test call
    if not YOUTUBE_API_KEY:
        youtube_status = "no_key"
    else:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"key": YOUTUBE_API_KEY, "id": "dQw4w9WgXcQ", "part": "id"},
                    timeout=6,
                )
            if r.status_code == 200:
                youtube_status = "live"
            else:
                data = r.json()
                msg = data.get("error", {}).get("message", f"HTTP {r.status_code}")
                youtube_status = f"error: {msg}"
        except Exception as e:
            youtube_status = f"error: {str(e)}"

    # TikTok: check if port 7890 proxy is reachable
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 7890), timeout=3
        )
        writer.close()
        await writer.wait_closed()
        tiktok_status = "live"
    except asyncio.TimeoutError:
        tiktok_status = "no_proxy"
    except ConnectionRefusedError:
        tiktok_status = "no_proxy"
    except Exception as e:
        tiktok_status = f"error: {str(e)}"

    return {
        "status": "ok",
        "youtube_status": youtube_status,
        "tiktok_status": tiktok_status,
        "whisper_loaded": _whisper_model is not None,
        "timestamp": datetime.utcnow().isoformat(),
    }

@app.post("/lookup-variants")
async def lookup_variants(req: LookupRequest):
    kw = req.keyword.strip()
    claude_error = None
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = await asyncio.to_thread(client.messages.create,
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role":"user","content":
                    f"For the game, anime, or media title '{kw}', return the official localized names used in each of these regions as a JSON array. "
                    f"Each item should have: language (English name of the language), code (ISO 639-1 language code), and name (the official local name). "
                    f"Regions: Japan, South Korea, China, Indonesia, Thailand, Vietnam, Arabic countries, India, Global English. "
                    f"If the title is not known in a region or uses the same name, still include it. Return ONLY valid JSON array, no explanation."
                }]
            )
            raw = msg.content[0].text.strip()
            print(f"[Claude] raw response for '{kw}': {raw[:200]}")
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
            variants = json.loads(raw.strip())
            for v in variants:
                v["flag"] = FLAG_MAP.get(v.get("code",""), "🌐")
            print(f"[Claude] lookup '{kw}' -> {len(variants)} variants")
            return {"keyword": kw, "variants": variants, "source": "claude"}
        except Exception as e:
            claude_error = str(e)
            print(f"[Claude] lookup failed: {e}")
    # Fallback to hardcoded table
    kw_lower = kw.lower()
    variants = FALLBACK_VARIANTS.get(kw_lower, [])
    return {"keyword": kw, "variants": variants, "source": "fallback", "claude_error": claude_error}

@app.post("/search")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks):
    if not req.keywords: raise HTTPException(400,"At least one keyword required")
    if not req.platforms: raise HTTPException(400,"At least one platform required")
    job_id=str(uuid.uuid4())
    jobs[job_id]={"id":job_id,"status":"queued","progress":0,"request":req.model_dump(),
                  "created_at":datetime.utcnow().isoformat(),"results":[],"summary":{}}
    background_tasks.add_task(run_search_job,job_id,req)
    return {"job_id":job_id,"status":"queued"}

@app.get("/jobs/{job_id}")
def get_job(job_id):
    if job_id not in jobs: raise HTTPException(404,"Job not found")
    job=jobs[job_id]
    if job["status"]!="done": return {k:v for k,v in job.items() if k!="results"}
    return job

@app.get("/jobs/{job_id}/export")
def export_job(job_id, fmt_param: str = "json"):
    if job_id not in jobs: raise HTTPException(404,"Job not found")
    results=jobs[job_id].get("results",[])
    if fmt_param=="csv":
        import csv,io; buf=io.StringIO()
        keys=["platform","id","url","title","views","likes","comments","shares",
              "engagement_rate","spoken_language","spoken_language_name",
              "upload_date","account.username","account.followers","keyword","region","hashtags"]
        w=csv.DictWriter(buf,fieldnames=keys,extrasaction="ignore"); w.writeheader()
        for r in results:
            row={**r,"account.username":r["account"]["username"],
                 "account.followers":r["account"]["followers"],
                 "hashtags":" ".join(r.get("hashtags",[]))}
            w.writerow({k:row.get(k,"") for k in keys})
        return Response(content=buf.getvalue(),media_type="text/csv",
                        headers={"Content-Disposition":f"attachment; filename=socialscope_{job_id[:8]}.csv"})
    return results

@app.post("/account")
async def account_lookup(req: AccountRequest):
    handle=req.handle.lstrip("@"); result={}
    if "youtube" in req.platforms and YOUTUBE_API_KEY:
        async with httpx.AsyncClient() as client:
            r=await client.get("https://www.googleapis.com/youtube/v3/channels",
                params={"key":YOUTUBE_API_KEY,"forHandle":handle,"part":"snippet,statistics"},timeout=10)
            items=r.json().get("items",[])
            if items:
                ch=items[0]; s,sn=ch.get("statistics",{}),ch.get("snippet",{})
                result["youtube"]={"id":ch["id"],"display_name":sn.get("title",""),
                    "description":sn.get("description","")[:300],
                    "subscribers":int(s.get("subscriberCount",0)),
                    "total_views":int(s.get("viewCount",0)),"video_count":int(s.get("videoCount",0)),
                    "country":sn.get("country",""),"profile_url":f"https://youtube.com/@{handle}",
                    "thumbnail":sn.get("thumbnails",{}).get("medium",{}).get("url","")}
    if "tiktok" in req.platforms:
        result["tiktok"]={"note":"TikTok account lookup requires a live TikTokApi session"}
    return result

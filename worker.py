import os
import sys
import json
import time
import requests
import subprocess
import glob
import re
import firebase_admin
from firebase_admin import credentials, firestore, db
import pysubs2
from requests_toolbelt.multipart.encoder import MultipartEncoder
from faster_whisper import WhisperModel
import urllib.parse
import concurrent.futures
from deep_translator import GoogleTranslator

# --- 🗣️ SPOKEN SINHALA DICTIONARY ---
try:
    from spoken_dict import SPOKEN_DICT
except ImportError:
    SPOKEN_DICT = {}

def apply_spoken_sinhala(text):
    if not text or not SPOKEN_DICT: return text
    sorted_keys = sorted(SPOKEN_DICT.keys(), key=len, reverse=True)
    result_text = str(text)
    for key in sorted_keys:
        value = SPOKEN_DICT[key]
        pattern = r'(?<![\w\u0D80-\u0DFF])' + re.escape(key) + r'(?![\w\u0D80-\u0DFF])'
        result_text = re.sub(pattern, value, result_text)
    return result_text

# --- ⚙️ SETUP FIREBASE ---
cred = credentials.Certificate("serviceAccountKey.json")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://anishift-5d14b-default-rtdb.firebaseio.com")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
fs_db = firestore.client()

# Bot 1 Database Node
RTDB_WORKER_FEEDBACK = "worker_job_status_short"

payload = json.loads(os.environ.get("JOB_PAYLOAD", "{}"))
anime_id = payload.get("anilist_id")
ep_num = payload.get("episode")
magnet = payload.get("magnet")
job_type = payload.get("job_type")
search_type = payload.get("search_type")
category = payload.get("category", "tv")
anime_title = payload.get("title", "Unknown Anime")

# Dynamic Abyss account credentials with env fallback
abyss_obj = payload.get("abyss") or {}
ABYSS_API_KEY = payload.get("abyss_api_key") or abyss_obj.get("key") or os.environ.get("ABYSS_API_KEY", "")
ABYSS_EMAIL = payload.get("abyss_email") or abyss_obj.get("email") or os.environ.get("ABYSS_EMAIL", "")       
ABYSS_PASSWORD = payload.get("abyss_password") or abyss_obj.get("password") or os.environ.get("ABYSS_PASSWORD", "") 
ABYSS_ACCOUNT_NAME = payload.get("abyss_account_name") or abyss_obj.get("name", "Default")
ABYSS_ACCOUNT_ID = payload.get("abyss_account_id") or abyss_obj.get("id", "")

DEDICATED_RTDB_URL = payload.get("rtdb_url") or abyss_obj.get("rtdb_url") or os.environ.get("FIREBASE_DB_URL", "https://anihsift-sever-2-default-rtdb.firebaseio.com")
ABYSS_UPLOAD_URL = f"https://up.abyss.to/{ABYSS_API_KEY}"

safe_anime_title = re.sub(r'[\\/*?:"<>|]', "", anime_title).strip()
print(f"🚀 [WORKER STARTED - V21 BOT-1 ABYSS ONLY] Anime: {safe_anime_title} | Ep: {ep_num} | Account: {ABYSS_ACCOUNT_NAME}", flush=True)

BASE_DIR = "downloads"
TEMP_SUB_DIR = f"temp_subs_ep_{ep_num}"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_SUB_DIR, exist_ok=True)

def notify_status(status="failed", file_size=0, file_code=None):
    try:
        p_key = payload.get("job_key")
        job_key = p_key if p_key else f"{anime_id}_ep_{ep_num}"
        fb_data = {
            "status": status,
            "anilist_id": str(anime_id),
            "episode": int(ep_num),
            "file_size": file_size,
            "file_code": file_code,
            "video_id": file_code,
            "job_type": job_type,
            "job_key": job_key,
            "account_id": ABYSS_ACCOUNT_ID,
            "account_name": ABYSS_ACCOUNT_NAME,
            "timestamp": time.time()
        }
        # 1. Direct REST PUT to Dedicated RTDB (0% Firestore reads, high reliability)
        if DEDICATED_RTDB_URL:
            try:
                clean_url = DEDICATED_RTDB_URL.rstrip('/')
                requests.put(f"{clean_url}/worker_feedback/{job_key}.json", json=fb_data, timeout=5)
                fallback_key = f"{anime_id}_ep_{ep_num}"
                if job_key != fallback_key:
                    requests.put(f"{clean_url}/worker_feedback/{fallback_key}.json", json=fb_data, timeout=5)
                requests.put(f"{clean_url}/worker_job_status_short/{job_key}.json", json=fb_data, timeout=5)
            except Exception: pass

        # 2. Legacy firebase_admin SDK update if available
        try:
            db.reference(RTDB_WORKER_FEEDBACK).child(job_key).set(fb_data)
            db.reference(RTDB_WORKER_FEEDBACK).update({
                "status": status,
                "anilist_id": str(anime_id),
                "episode": int(ep_num),
                "file_size": file_size,
                "file_code": file_code,
                "timestamp": time.time()
            })
        except Exception: pass
    except: pass


def extract_ep_number(filename):
    clean = re.sub(r'\[.*?\]|\(.*?\)', ' ', filename.lower())
    clean = re.sub(r'\b(1080p|720p|480p|x264|x265|hevc|10bit|8bit)\b', ' ', clean)
    m = re.search(r'[sS]\d+[eE]0*(\d+)', clean)
    if m: return int(m.group(1))
    m = re.search(r'\b(?:ep|episode)\.?\s?0*(\d+)\b', clean)
    if m: return int(m.group(1))
    m = re.search(r'(?:\s-\s|_|#\s?)0*(\d+)(?:v\d)?(?:\b|_)', clean)
    if m: return int(m.group(1))

    clean_no_season = re.sub(r'\b(?:s|season|series)\s?\d+\b', ' ', clean, flags=re.IGNORECASE)
    clean_no_season = re.sub(r'\b\d+(?:st|nd|rd|th)\s?season\b', ' ', clean_no_season, flags=re.IGNORECASE)
    m = re.search(r'\b0*(\d+)\b', clean_no_season)
    if m: return int(m.group(1))
    return None

def detect_encoding(file_path):
    for enc in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                f.read(1024)
            return enc
        except Exception:
            continue
    return 'utf-8'

def clean_vtt_tags(text):
    if not text: return ""
    text = re.sub(r'\{.*?\}', '', text).replace('\\h', ' ')
    return re.sub(r'<[^>]+>', '', text).strip()

def is_garbage_sub(text):
    if not text: return True
    if re.search(r'\\pos\(|\\c&H|\\alpha|\\t\(|\\fad\(|\\an\d', text): return True
    cl = re.sub(r'<[^>]+>', '', re.sub(r'\{.*?\}', '', text)).strip()
    if re.match(r'^m\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+(?:l|b|s|c|m)\s+', cl): return True
    return False

def has_sinhala_characters(text):
    return bool(re.search(r'[\u0D80-\u0DFF]', str(text)))

def has_letters(text):
    return bool(re.search(r'[a-zA-Z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]', str(text)))

WARP_PROXIES = {
    "http": "socks5://127.0.0.1:40000",
    "https": "socks5://127.0.0.1:40000"
}

def translate_guaranteed_sinhala(text):
    if not text or len(text.strip()) == 0: return ""
    if not has_letters(text): return text

    for macro_attempt in range(2): 
        for attempt in range(2):
            try:
                translator = GoogleTranslator(source='auto', target='si', proxies=WARP_PROXIES)
                res = translator.translate(text)
                if res and has_sinhala_characters(res):
                    return apply_spoken_sinhala(res)
            except Exception: time.sleep(1)

        try:
            url = "https://clients5.google.com/translate_a/t"
            params = {"client": "dict-chrome-ex", "sl": "auto", "tl": "si", "q": text}
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, params=params, headers=headers, proxies=WARP_PROXIES, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    res_text = str(data[0][0]) if isinstance(data[0], list) else str(data[0])
                    if res_text and has_sinhala_characters(res_text):
                        return apply_spoken_sinhala(res_text)
        except: pass

        try:
            translator = GoogleTranslator(source='auto', target='si')
            res = translator.translate(text)
            if res and has_sinhala_characters(res):
                return apply_spoken_sinhala(res)
        except: pass
        time.sleep(1)

    return ""

def download_video():
    print(f"📥 Starting Download...", flush=True)
    timeout_arg = '--bt-stop-timeout=300'
    
    if search_type == "BATCH":
        subprocess.run(['aria2c', '--bt-metadata-only=true', '--bt-save-metadata=true', '--seed-time=0', '--bt-stop-timeout=120', magnet])
        torrent_files = glob.glob("*.torrent")
        if torrent_files:
            from torrentool.api import Torrent
            my_torrent = Torrent.from_file(torrent_files[0])
            target_idx = None
            for idx, f in enumerate(my_torrent.files, start=1):
                if f.name.lower().endswith(('.mkv', '.mp4')) and extract_ep_number(os.path.basename(f.name)) == int(ep_num):
                    target_idx = idx
                    break
            if target_idx:
                subprocess.run(['aria2c', '--seed-time=0', f'--select-file={target_idx}', f'--dir={BASE_DIR}', timeout_arg, torrent_files[0]])
    else:
        subprocess.run(['aria2c', '--seed-time=0', f'--dir={BASE_DIR}', timeout_arg, magnet])

    target_ep_int = int(ep_num)
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')) and extract_ep_number(f) == target_ep_int:
                return os.path.join(root, f)
    for root, dirs, files in os.walk(BASE_DIR):
        for f in files:
            if f.endswith(('.mkv', '.mp4')): return os.path.join(root, f)
    return None

def extract_and_score_subtitles(video_path):
    print("🔍 Scanning video for softsubs...", flush=True)
    eng_sub_path = os.path.join(TEMP_SUB_DIR, "extracted.srt")
    
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries', 'stream=index:stream_tags=language:stream_tags=title', '-of', 'json', video_path]
    
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        streams = json.loads(result.stdout).get('streams', [])
        if not streams: return None

        valid_subs_data = []
        for s in streams:
            idx = s['index']
            lang = s.get('tags', {}).get('language', '').lower()
            title = s.get('tags', {}).get('title', '').lower()
            
            temp_sub = os.path.join(TEMP_SUB_DIR, f"temp_track_{idx}.srt")
            subprocess.run(['ffmpeg', '-i', video_path, '-map', f'0:{idx}', temp_sub, '-y'], stderr=subprocess.DEVNULL)
            
            if os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 100:
                try:
                    try: subs = pysubs2.load(temp_sub, encoding='utf-8')
                    except: subs = pysubs2.load(temp_sub, encoding='latin-1')
                        
                    line_count = len(subs.events)
                    score = line_count
                    if line_count >= 150:
                        if lang == 'en' or 'eng' in title or 'english' in title: score += 100000 
                        elif lang == 'ja' or 'jap' in title or 'romaji' in title: score -= 100000 
                    else: score -= 50000 
                        
                    valid_subs_data.append({'index': idx, 'path': temp_sub, 'lines': line_count, 'score': score, 'name': title})
                except Exception: pass

        if valid_subs_data:
            valid_subs_data.sort(key=lambda x: x['score'], reverse=True)
            best_sub = valid_subs_data[0]
            if best_sub['lines'] >= 150:
                print(f"🏆 WINNER: Stream {best_sub['index']} ('{best_sub['name']}') with {best_sub['lines']} lines!", flush=True)
                os.rename(best_sub['path'], eng_sub_path)
                for loser in valid_subs_data[1:]:
                    if os.path.exists(loser['path']): os.remove(loser['path'])
                return eng_sub_path
                
    except Exception: pass
    return None

def process_sinhala_sub(sub_path):
    out_name = os.path.join(TEMP_SUB_DIR, "sinhala_sub.srt")
    try:
        print("🧹 Cleaning dialogs & unwanted lines...", flush=True)
        try: subs = pysubs2.load(sub_path, encoding=detect_encoding(sub_path))
        except: subs = pysubs2.load(sub_path, encoding='latin-1')
        
        cleaned_events, unique_texts, prev_text, seen_texts_count = [], set(), "", {}
        bad_words = ['subtitle by', 'translated by', 'sync by', 'encoded by', 'www.', '.com', 'discord', 'telegram', 'netlify', 'anishift', 'download කිරීමට', 'නැරඹීමට']
        
        for e in subs:
            if is_garbage_sub(e.text): continue
            txt, t_low = clean_vtt_tags(e.text), clean_vtt_tags(e.text).lower()
            if any(x in t_low for x in bad_words) or len(txt) > 250 or len(txt) < 2 or '♪' in txt or '♫' in txt: continue
            
            if txt == prev_text:
                if cleaned_events: cleaned_events[-1].end = max(cleaned_events[-1].end, e.end)
                continue
                
            seen_texts_count[txt] = seen_texts_count.get(txt, 0) + 1
            if len(txt) > 30 and seen_texts_count[txt] > 2: continue
            
            e.text = txt
            cleaned_events.append(e)
            unique_texts.add(txt)
            prev_text = txt
            
        if not cleaned_events: return None
        
        uni_list = list(unique_texts)
        total_lines = len(uni_list)
        print(f"🚀 Translating {total_lines} lines (Strict Sinhala Mode ⚡)...", flush=True)
        
        translation_map = {}
        def process_single(text): return text, translate_guaranteed_sinhala(text)

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(process_single, t) for t in uni_list]
            done_lines = 0
            for future in concurrent.futures.as_completed(futures):
                orig, trans = future.result()
                translation_map[orig] = trans
                done_lines += 1
                if done_lines % 25 == 0 or done_lines == total_lines:
                    print(f"   📊 Progress: {int((done_lines/total_lines)*100)}%", flush=True)
                    
        final_events = []
        for event in cleaned_events: 
            translated_text = translation_map.get(event.text, event.text)
            if translated_text != "": 
                event.text = translated_text
                final_events.append(event)
            
        subs.events = final_events
        subs.save(out_name, encoding="utf-8")
        return out_name
    except Exception as e:
        print(f"❌ Error: {e}", flush=True)
        return None

def process_and_translate_subtitle(video_path):
    eng_sub = os.path.join(TEMP_SUB_DIR, "extracted.srt") 
    extracted_successfully = False

    best_sub_path = extract_and_score_subtitles(video_path)
    if best_sub_path and os.path.exists(best_sub_path):
        extracted_successfully = True
        eng_sub = best_sub_path

    if not extracted_successfully:
        print("⚠️ Starting AI Audio Transcription as fallback (small model)...", flush=True)
        audio_path = os.path.join(TEMP_SUB_DIR, "audio.mp3")
        subprocess.run(['ffmpeg', '-i', video_path, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', audio_path, '-y'], stderr=subprocess.DEVNULL)
        if os.path.exists(audio_path):
            try:
                model = WhisperModel("small", device="cpu", compute_type="int8")
                segments, info = model.transcribe(audio_path, task="translate", vad_filter=True, beam_size=5)
                subs = pysubs2.SSAFile()
                for segment in segments:
                    subs.events.append(pysubs2.SSAEvent(start=int(segment.start * 1000), end=int(segment.end * 1000), text=segment.text.strip()))
                subs.save(eng_sub, encoding="utf-8")
                extracted_successfully = True
            except Exception: pass
        
    if not extracted_successfully: return None
    return process_sinhala_sub(eng_sub)

def get_abyss_token():
    print("🔑 Authenticating with Abyss...", flush=True)
    if not ABYSS_EMAIL or not ABYSS_PASSWORD: return None
    try:
        res = requests.post("https://api.abyss.to/auth/login", json={"email": ABYSS_EMAIL, "password": ABYSS_PASSWORD}).json()
        return res.get("token")
    except Exception: return None

def upload_video_to_abyss(video_path):
    print("☁️ Uploading Video to Abyss.to...", flush=True)
    upload_filename = os.path.basename(video_path)
    mime_type = 'video/x-matroska' if upload_filename.endswith('.mkv') else 'video/mp4'

    for attempt in range(3):
        try:
            fields = {'file': (upload_filename, open(video_path, 'rb'), mime_type)}
            multipart_data = MultipartEncoder(fields=fields)
            headers = {'Content-Type': multipart_data.content_type, 'User-Agent': 'Mozilla/5.0'}

            up_resp = requests.post(ABYSS_UPLOAD_URL, data=multipart_data, headers=headers, timeout=1200)
            try: resp_data = up_resp.json()
            except: 
                if attempt < 2: time.sleep(15)
                continue

            if str(resp_data.get("status")) in ["True", "200", "true"]:
                vhd_code = resp_data.get("slug") or resp_data.get("id") or resp_data.get("code")
                if vhd_code: return vhd_code, os.path.getsize(video_path)
        except:
            if attempt < 2: time.sleep(15)
    return None, 0

def upload_subtitle_to_abyss_api(vhd_code, srt_path, token):
    print("☁️ Uploading Sinhala Subtitle to Abyss...", flush=True)
    try:
        url = f"https://api.abyss.to/v1/upload/subtitles/{vhd_code}?language=Sinhala&filename=sinhala.srt"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"} 
        with open(srt_path, "rb") as f: sub_data = f.read()
        resp = requests.put(url, headers=headers, data=sub_data, timeout=60)
        if resp.status_code == 200: print("🎉 Subtitle Attached Successfully!", flush=True)
    except Exception: pass


# ==========================================
# 💾 FIRESTORE UPDATE (Abyss Only)
# ==========================================
def update_database(file_code):
    print("💾 Updating Firestore with Abyss link...", flush=True)
    ep_doc_id = f"episode_{int(ep_num):04d}" if str(ep_num).isdigit() else f"episode_{ep_num}"
    
    data = {
        'status': 'uploaded',
        'links': {
            'abyss_video_id': file_code, 
            'abyss_embed': f"https://abyss.to/embed/{file_code}",
            'account': ABYSS_ACCOUNT_NAME
        },
        'account_name': ABYSS_ACCOUNT_NAME,
        'server_3_uploaded': True,
        'last_updated': firestore.SERVER_TIMESTAMP
    }
    
    col_name = 'anime_movies' if (category == 'movie' or job_type == 'movie') else 'anime_series'
    try:
        fs_db.collection(col_name).document(str(anime_id)).collection('episodes').document(ep_doc_id).set(data, merge=True)
        print(f"✅ Firestore Updated in {col_name}!", flush=True)
    except Exception as e:
        print(f"⚠️ Firestore update error: {e}", flush=True)

def cleanup_temp_files():
    print("🧹 Cleaning up downloaded video and temporary files to free disk space...", flush=True)
    import shutil
    try:
        if os.path.exists(BASE_DIR):
            shutil.rmtree(BASE_DIR, ignore_errors=True)
            os.makedirs(BASE_DIR, exist_ok=True)
    except Exception: pass

    try:
        if os.path.exists(TEMP_SUB_DIR):
            shutil.rmtree(TEMP_SUB_DIR, ignore_errors=True)
    except Exception: pass

    for ext in ("*.torrent", "*.mkv", "*.mp4", "*.srt", "*.vtt", "*.mp3", "*.aria2"):
        for f in glob.glob(ext):
            try: os.remove(f)
            except Exception: pass

# --- MAIN EXECUTION ---
original_video = download_video()

if original_video:
    srt_sub_path = process_and_translate_subtitle(original_video)
    jwt_token = get_abyss_token()
    
    print("✂️ Processing Dual-Audio & removing internal subtitles...", flush=True)
    original_filename = os.path.basename(original_video)
    clean_video = os.path.join(TEMP_SUB_DIR, original_filename)
    
    # 🔥 DUAL-AUDIO SMART LOGIC 
    try:
        probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index:stream_tags=language:stream_tags=title', '-of', 'json', original_video]
        probe_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        audio_streams = json.loads(probe_res.stdout).get('streams', [])
        
        audio_map = ['-map', '0:a?'] 
        
        if len(audio_streams) > 1:
            print(f"🔊 Dual-Audio detected! ({len(audio_streams)} audio tracks). Finding Japanese track...", flush=True)
            jpn_index = None
            non_eng_index = None
            
            for s in audio_streams:
                lang = s.get('tags', {}).get('language', '').lower()
                title = s.get('tags', {}).get('title', '').lower()
                
                if lang in ['ja', 'jpn', 'japanese'] or 'japanese' in title or '日本語' in title or 'nihongo' in title:
                    jpn_index = s['index']
                    break
                
                if lang not in ['en', 'eng', 'english'] and 'english' not in title and non_eng_index is None:
                    non_eng_index = s['index']
            
            if jpn_index is not None:
                audio_map = ['-map', f'0:{jpn_index}']
            elif non_eng_index is not None:
                audio_map = ['-map', f'0:{non_eng_index}']
            else:
                audio_map = ['-map', '0:a:0']
        else:
            audio_map = ['-map', '0:a:0?']
            
        ff_cmd = ['ffmpeg', '-i', original_video, '-map', '0:v:0'] + audio_map + ['-c', 'copy', '-sn', clean_video, '-y']
        subprocess.run(ff_cmd, stderr=subprocess.DEVNULL)
        
    except Exception as e:
        print(f"⚠️ Audio parsing failed, falling back to basic cleanup...", flush=True)
        subprocess.run(['ffmpeg', '-i', original_video, '-c', 'copy', '-sn', clean_video, '-y'], stderr=subprocess.DEVNULL)
    
    # වීඩියෝ එක Abyss එකට අප්ලෝඩ් කිරීම
    video_to_upload = clean_video if os.path.exists(clean_video) else original_video
    upload_result = upload_video_to_abyss(video_to_upload)
    
    if upload_result and upload_result[0]:
        file_code, file_size = upload_result
        
        # සබ් එක Abyss එකට ඇටෑච් කිරීම
        if srt_sub_path and os.path.exists(srt_sub_path) and jwt_token:
            upload_subtitle_to_abyss_api(file_code, srt_sub_path, jwt_token)
            
        # Database එක අප්ඩේට් කිරීම
        update_database(file_code)
        
        notify_status("success", file_size, file_code=file_code)
        print("🎉 WORKER COMPLETED SUCCESSFULLY (Abyss Upload Only)!", flush=True)
        cleanup_temp_files()
        sys.exit(0)
    else:
        print("❌ Video Upload Failed!", flush=True)
        notify_status("failed", 0)
        cleanup_temp_files()
        sys.exit(1)
else:
    print("❌ Download Failed!", flush=True)
    notify_status("failed", 0)
    cleanup_temp_files()
    sys.exit(1)


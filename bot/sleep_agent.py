import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Dict, List, Any, Optional

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT_FILE = os.path.join(BOT_DIR, "sleep_vault.json")

SSL_CTX = ssl.create_default_context()
SSL_UNVERIFIED_CTX = ssl._create_unverified_context()

CST = timezone(timedelta(hours=-6))

SLEEP_MEMORY: Dict[int, List[Dict[str, Any]]] = {}
SLEEP_BATCH_LOCK = threading.Lock()
SLEEP_BATCH: Dict[int, Dict[str, Any]] = {}
SLEEP_BATCH_DELAY = 2.0

SLEEP_CLOUD_DONE = False
SLEEP_CLOUD_LOCK = threading.Lock()
SLEEP_STATE_CACHE: Dict[str, Any] = {
    "medication_logs": [],
    "notes": [],
    "rules": [],
    "hunger_logs": {},
    "alcohol_logs": {},
    "nicotine_logs": {},
    "oura_tokens": {},
    "oura_cache": {},
}
SLEEP_VAULT_CACHE: Dict[str, Any] = {}
SLEEP_SEEN_UPDATES = set()
OURA_SYNC_RUNNING = False
ALLOWED_CHAT_IDS = {8299631714, 8349556362}


def now_cst() -> datetime:
    return datetime.now(timezone.utc).astimezone(CST)


def format_seconds(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


def _open_url(req, timeout: int = 25):
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            return urllib.request.urlopen(req, timeout=timeout, context=SSL_UNVERIFIED_CTX)
        raise


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def load_vault(gemini_key: str) -> Dict[str, Any]:
    global SLEEP_VAULT_CACHE
    if SLEEP_VAULT_CACHE:
        return SLEEP_VAULT_CACHE
    if not gemini_key or not os.path.exists(VAULT_FILE):
        return {}
    try:
        with open(VAULT_FILE, "r", encoding="utf-8") as f:
            enc = json.load(f)
        salt = base64.b64decode(enc["salt"])
        nonce = base64.b64decode(enc["nonce"])
        ct = base64.b64decode(enc["ct"])
        tag = base64.b64decode(enc["tag"])
        derived = hashlib.pbkdf2_hmac("sha256", gemini_key.encode("utf-8"), salt, 100000, dklen=64)
        enc_key, mac_key = derived[:32], derived[32:]
        expected_tag = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            print("[sleep] Vault HMAC mismatch!", flush=True)
            return {}
        stream = _keystream(enc_key, nonce, len(ct))
        pt = bytes(a ^ b for a, b in zip(ct, stream))
        if enc.get("compressed"):
            try:
                pt = zlib.decompress(pt)
            except Exception:
                pass
        SLEEP_VAULT_CACHE = json.loads(pt.decode("utf-8"))
        return SLEEP_VAULT_CACHE
    except Exception as e:
        print(f"[sleep] Vault load failed: {e}", flush=True)
        return {}


def get_sleep_token(gemini_key: str) -> str:
    env_tok = os.environ.get("SLEEP_BOT_TOKEN", "").strip()
    if env_tok:
        return env_tok
    vault = load_vault(gemini_key)
    return str(vault.get("token", "")).strip()


def tg_api(token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with _open_url(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(token: str, chat_id: int, text: str):
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [text]
    for chunk in chunks:
        tg_api(token, "sendMessage", {"chat_id": chat_id, "text": chunk})


def save_state_to_cloud(token: str, data: Dict[str, Any]):
    if not token:
        return
    # Keep setMyCommands compact & within Telegram's size limits by excluding large oura_cache (kept in RAM & vault)
    compact_data = {k: v for k, v in data.items() if k != "oura_cache"}
    raw_bytes = json.dumps(compact_data, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw_bytes, level=9)
    b64_str = base64.b64encode(compressed).decode("ascii")
    chunks = [b64_str[i : i + 240] for i in range(0, len(b64_str), 240)][:95]
    # Telegram BotCommand description must be 3-256 chars; pad short trailing chunks
    commands = [{"command": f"s{idx}", "description": ch.ljust(3)} for idx, ch in enumerate(chunks)]
    payload = {
        "commands": commands,
        "scope": {"type": "all_chat_administrators"},
        "language_code": "eo",
    }
    for attempt in range(2):
        try:
            tg_api(token, "setMyCommands", payload)
            return
        except Exception as e:
            print(f"[sleep] Cloud save attempt {attempt + 1} failed: {e}", flush=True)
            time.sleep(0.5)


def sync_state_from_cloud(token: str) -> Dict[str, Any]:
    global SLEEP_CLOUD_DONE, SLEEP_STATE_CACHE
    if SLEEP_CLOUD_DONE:
        return SLEEP_STATE_CACHE
    with SLEEP_CLOUD_LOCK:
        if SLEEP_CLOUD_DONE:
            return SLEEP_STATE_CACHE
        if not token:
            SLEEP_CLOUD_DONE = True
            return SLEEP_STATE_CACHE
        try:
            res = tg_api(
                token,
                "getMyCommands",
                {
                    "scope": {"type": "all_chat_administrators"},
                    "language_code": "eo",
                },
            )
            cmds = res.get("result", [])
            if cmds and isinstance(cmds, list):
                sorted_cmds = sorted(
                    [c for c in cmds if str(c.get("command", "")).startswith("s")],
                    key=lambda x: int(str(x["command"])[1:] or "0"),
                )
                b64_str = "".join(str(c.get("description", "")).strip() for c in sorted_cmds)
                if b64_str:
                    raw_bytes = base64.b64decode(b64_str.encode("ascii"))
                    try:
                        json_bytes = zlib.decompress(raw_bytes)
                    except Exception:
                        json_bytes = raw_bytes
                    cloud_data = json.loads(json_bytes.decode("utf-8"))
                    if isinstance(cloud_data, dict):
                        SLEEP_STATE_CACHE = {
                            "medication_logs": list(cloud_data.get("medication_logs", [])),
                            "notes": list(cloud_data.get("notes", [])),
                            "rules": list(cloud_data.get("rules", [])),
                            "hunger_logs": dict(cloud_data.get("hunger_logs", {})),
                            "alcohol_logs": dict(cloud_data.get("alcohol_logs", {})),
                            "nicotine_logs": dict(cloud_data.get("nicotine_logs", {})),
                            "oura_tokens": dict(cloud_data.get("oura_tokens", {})),
                            "oura_cache": dict(cloud_data.get("oura_cache", {})),
                        }
        except Exception as e:
            print(f"[sleep] Cloud sync warning: {e}", flush=True)
        SLEEP_CLOUD_DONE = True
    return SLEEP_STATE_CACHE


def strip_markdown_asterisks(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("* "):
            line = line.replace("* ", "- ", 1)
        lines.append(line)
    return "\n".join(lines)


def extract_time_taken(text: str, fallback_dt: datetime) -> str:
    """Extract time taken from messages like '9:20pm', '920pm', '1030pm', 'at 9', '9pm'."""
    def _format_hm(hour: int, minute: int, ampm_raw: Optional[str]) -> Optional[str]:
        if minute < 0 or minute > 59 or hour < 0 or hour > 23:
            return None
        if ampm_raw:
            ap = ampm_raw.upper()
            h12 = hour % 12 or 12
            return f"{h12}:{minute:02d} {ap}"
        if hour >= 13:
            h12 = hour - 12
            return f"{h12}:{minute:02d} PM"
        if hour == 0:
            return f"12:{minute:02d} AM"
        # No am/pm specified on 1-12 hour: sleep meds are almost always PM (except 12 -> AM/midnight or 1-4 AM)
        ap = "AM" if (hour == 12 or 1 <= hour <= 4) else "PM"
        return f"{hour}:{minute:02d} {ap}"

    # 1. Colon format: 9:20pm, 9:20 PM, 21:20, at 9:20
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text, re.IGNORECASE)
    if m:
        res = _format_hm(int(m.group(1)), int(m.group(2)), m.group(3))
        if res:
            return res

    # 2. Compact 3-4 digit time with am/pm OR preceded by at/@: 920pm, 1030pm, at 920, @ 844
    m = re.search(r"\b(\d{3,4})\s*(am|pm)\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(?:at|@)\s*(\d{3,4})\b", text, re.IGNORECASE)
    if m:
        digits = m.group(1)
        ampm = m.group(2) if m.lastindex and m.lastindex >= 2 else None
        h = int(digits[:-2])
        mins = int(digits[-2:])
        res = _format_hm(h, mins, ampm)
        if res:
            return res

    # 3. Hour-only with am/pm OR preceded by at/@: 9pm, at 9, @ 10
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(?:at|@)\s*(\d{1,2})\b", text, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        ampm = m.group(2) if m.lastindex and m.lastindex >= 2 else None
        res = _format_hm(h, 0, ampm)
        if res:
            return res

    # Fallback to actual message timestamp instead of hardcoded 9:00 PM
    return fallback_dt.strftime("%I:%M %p").lstrip("0")


def parse_and_record_dose(text: str, gemini_key: str, token: str) -> Optional[str]:
    text_clean = text.strip()
    text_lower = text_clean.lower()

    now = now_cst()
    time_taken_str = extract_time_taken(text_clean, now)

    traz_mg = None
    quet_mg = None

    if "repeat" in text_lower or "same" in text_lower:
        state = sync_state_from_cloud(token)
        vault = load_vault(gemini_key)
        files = vault.get("files", {})
        all_logs = []
        try:
            base_logs = json.loads(files.get("data/medication_logs.json", "{}")).get("logs", [])
            all_logs.extend(base_logs)
        except Exception:
            pass
        all_logs.extend(state.get("medication_logs", []))
        if all_logs:
            last = all_logs[-1]
            traz_mg = last.get("trazodone_mg", 100)
            quet_mg = last.get("quetiapine_mg", 100)
        else:
            traz_mg = 100
            quet_mg = 100

    if traz_mg is None:
        t_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mg)?\s*(?:traz|trazodone|t\b)", text_lower)
        if t_m:
            traz_mg = int(float(t_m.group(1)))

    if quet_mg is None:
        q_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mg)?\s*(?:quet|quetiapine|seroquel|q\b)", text_lower)
        if q_m:
            quet_mg = int(float(q_m.group(1)))

    if traz_mg is None and quet_mg is None:
        split_m = re.search(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", text_lower)
        if split_m:
            traz_mg = int(split_m.group(1))
            quet_mg = int(split_m.group(2))

    if traz_mg is None and quet_mg is None:
        return None

    traz_val = f"{traz_mg}mg" if traz_mg is not None else "0mg"
    quet_val = f"{quet_mg}mg" if quet_mg is not None else "0mg"

    # Dose logged at night (or past midnight before noon) belongs to that sleep night
    if now.hour >= 12:
        night_of_str = now.strftime("%Y-%m-%d")
        date_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        night_of_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_str = now.strftime("%Y-%m-%d")

    state = sync_state_from_cloud(token)
    med_logs = state.setdefault("medication_logs", [])
    existing_entry = next(
        (m for m in med_logs if m.get("night_of") == night_of_str or m.get("date") == date_str),
        None,
    )
    alc_val = (state.get("alcohol_logs") or {}).get(night_of_str) or (existing_entry or {}).get("alcohol") or "No"
    if "alcohol" in text_lower:
        alc_val = "No" if re.search(r"\b(?:no|none|0|false)\b", text_lower) else "Yes"
    state.setdefault("alcohol_logs", {})[night_of_str] = alc_val

    new_entry = {
        "date": date_str,
        "night_of": night_of_str,
        "trazodone_mg": traz_mg or 0,
        "quetiapine_mg": quet_mg or 0,
        "time_taken": time_taken_str,
        "alcohol": alc_val,
        "notes": f"Logged via cloud webhook: {text_clean}"
    }
    if existing_entry:
        for k in ("hunger_level", "nicotine"):
            if existing_entry.get(k):
                new_entry[k] = existing_entry[k]
    replaced = False
    for i, existing in enumerate(med_logs):
        if existing.get("night_of") == night_of_str or existing.get("date") == date_str:
            med_logs[i] = new_entry
            replaced = True
            break
    if not replaced:
        med_logs.append(new_entry)
    save_state_to_cloud(token, state)

    return (
        f"Roger Roger\n"
        f"Trazadone Dosage: {traz_val}\n"
        f"Quetiapine Dosage: {quet_val}\n"
        f"Date: {night_of_str}\n"
        f"Time taken: {time_taken_str}"
    )


def sync_oura_cloud(gemini_key: str, token: str) -> str:
    """Syncs Oura Ring data directly from Render cloud, auto-refreshing OAuth tokens."""
    global OURA_SYNC_RUNNING
    if OURA_SYNC_RUNNING:
        return "Oura sync is already running in background."
    OURA_SYNC_RUNNING = True
    try:
        vault = load_vault(gemini_key)
        files = vault.get("files", {})
        state = sync_state_from_cloud(token)

        cred_raw = files.get("bot/.oura_credentials.json", "{}")
        tokens_raw = files.get("bot/.oura_tokens.json", "{}")
        try:
            credentials = json.loads(cred_raw)
        except Exception:
            credentials = {}
        try:
            tokens = state.get("oura_tokens") or json.loads(tokens_raw)
        except Exception:
            tokens = {}

        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_at = tokens.get("expires_at", 0)

        if not client_id or not client_secret or not refresh_token:
            return "Oura sync skipped: OAuth credentials not available in vault."

        now_ts = int(time.time())
        if now_ts >= (expires_at - 300) or not access_token:
            try:
                refresh_data = urllib.parse.urlencode({
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.ouraring.com/oauth/token",
                    data=refresh_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                with _open_url(req, timeout=20) as resp:
                    new_tokens = json.loads(resp.read().decode("utf-8"))
                access_token = new_tokens.get("access_token")
                refresh_token = new_tokens.get("refresh_token", refresh_token)
                expires_in = new_tokens.get("expires_in", 2592000)
                tokens = {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": expires_in,
                    "expires_at": int(time.time()) + expires_in,
                    "created_at": datetime.now().isoformat()
                }
                state["oura_tokens"] = tokens
                save_state_to_cloud(token, state)
            except Exception as e:
                return f"Failed to refresh Oura token: {e}"

        # Fetch daily sleep, readiness, and sleep sessions for last 7 days
        end_dt = now_cst() + timedelta(days=1)
        start_dt = end_dt - timedelta(days=8)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        headers = {"Authorization": f"Bearer {access_token}"}

        req1 = urllib.request.Request(f"https://api.ouraring.com/v2/usercollection/daily_sleep?start_date={start_str}&end_date={end_str}", headers=headers)
        with _open_url(req1, timeout=20) as r:
            sleep_docs = json.loads(r.read().decode("utf-8")).get("data", [])

        req2 = urllib.request.Request(f"https://api.ouraring.com/v2/usercollection/daily_readiness?start_date={start_str}&end_date={end_str}", headers=headers)
        with _open_url(req2, timeout=20) as r:
            readiness_docs = json.loads(r.read().decode("utf-8")).get("data", [])

        req3 = urllib.request.Request(f"https://api.ouraring.com/v2/usercollection/sleep?start_date={start_str}&end_date={end_str}", headers=headers)
        with _open_url(req3, timeout=20) as r:
            session_docs = json.loads(r.read().decode("utf-8")).get("data", [])

        cache = {}
        raw_cache = state.get("oura_cache") or files.get("data/oura_cache.json", "{}")
        try:
            cache = json.loads(raw_cache).get("daily_records", {}) if isinstance(raw_cache, str) else raw_cache.get("daily_records", {})
        except Exception:
            cache = {}

        for item in sleep_docs:
            day = item.get("day")
            if day:
                entry = cache.setdefault(day, {"date": day})
                entry["sleep_score"] = item.get("score")
                entry["sleep_contributors"] = item.get("contributors", {})

        for item in readiness_docs:
            day = item.get("day")
            if day:
                entry = cache.setdefault(day, {"date": day})
                entry["readiness_score"] = item.get("score")
                entry["readiness_contributors"] = item.get("contributors", {})
                if "resting_heart_rate" in item.get("contributors", {}):
                    entry["resting_heart_rate"] = item["contributors"]["resting_heart_rate"]

        for session in session_docs:
            day = session.get("day")
            if not day:
                continue
            entry = cache.setdefault(day, {"date": day})
            dur = session.get("total_sleep_duration", 0)
            if dur >= entry.get("total_sleep_seconds", 0):
                entry["total_sleep_seconds"] = dur
                entry["total_sleep_formatted"] = format_seconds(dur)
                deep = session.get("deep_sleep_duration", 0)
                entry["deep_sleep_seconds"] = deep
                entry["deep_sleep_formatted"] = format_seconds(deep)
                entry["deep_sleep_pct"] = round((deep / dur * 100), 1) if dur > 0 else 0
                rem = session.get("rem_sleep_duration", 0)
                entry["rem_sleep_seconds"] = rem
                entry["rem_sleep_formatted"] = format_seconds(rem)
                entry["rem_sleep_pct"] = round((rem / dur * 100), 1) if dur > 0 else 0
                light = session.get("light_sleep_duration", 0)
                entry["light_sleep_seconds"] = light
                entry["light_sleep_formatted"] = format_seconds(light)
                entry["light_sleep_pct"] = round((light / dur * 100), 1) if dur > 0 else 0
                awake = session.get("awake_time", 0)
                entry["awake_seconds"] = awake
                entry["awake_formatted"] = format_seconds(awake)
                entry["awake_mins"] = round(awake / 60) if awake else 0

                phases_5m = session.get("sleep_phase_5_min", "")
                if phases_5m:
                    mid_5m = phases_5m.lstrip("4").rstrip("4")
                    entry["wakeups"] = len(re.findall(r"4+", mid_5m))
                else:
                    entry["wakeups"] = entry.get("wakeups", 0)

                entry["sleep_efficiency"] = session.get("efficiency", 0)
                entry["lowest_heart_rate"] = session.get("lowest_heart_rate")
                entry["average_heart_rate"] = round(session.get("average_heart_rate", 0)) if session.get("average_heart_rate") else None
                entry["average_hrv"] = session.get("average_hrv")
                entry["bedtime_start"] = session.get("bedtime_start")
                entry["bedtime_end"] = session.get("bedtime_end")
                lat_sec = session.get("latency")
                if lat_sec is not None:
                    entry["latency_seconds"] = lat_sec
                    entry["latency_mins"] = round(lat_sec / 60)

        state["oura_cache"] = {"last_synced": now_cst().isoformat(), "daily_records": cache}
        save_state_to_cloud(token, state)
        latest_day = sorted(cache.keys())[-1] if cache else "—"
        latest_rec = cache.get(latest_day, {})
        return (
            f"Roger Roger.\n\n"
            f"Oura Ring Sync Complete ({len(cache)} days in cache).\n"
            f"Latest Night: {latest_day}\n"
            f"- Sleep Score: {latest_rec.get('sleep_score', '—')}\n"
            f"- Readiness: {latest_rec.get('readiness_score', '—')}\n"
            f"- Wakeups: {latest_rec.get('wakeups', '—')}\n"
            f"- Duration: {latest_rec.get('total_sleep_formatted', '—')}\n"
            f"- Deep: {latest_rec.get('deep_sleep_pct', '—')}%\n"
            f"- Avg HRV: {latest_rec.get('average_hrv', '—')} ms"
        )
    except Exception as e:
        return f"Oura sync failed: {e}"
    finally:
        OURA_SYNC_RUNNING = False


def _auto_sync_loop(gemini_key: str, token: str):
    """Background loop: automatically syncs fresh Oura records during morning hours."""
    time.sleep(30)
    while True:
        try:
            now = now_cst()
            # If morning (5:30 AM to 10:30 AM CST), check if today's record exists in cache
            if 5 <= now.hour <= 10:
                state = sync_state_from_cloud(token)
                today_str = now.strftime("%Y-%m-%d")
                cache = (state.get("oura_cache") or {}).get("daily_records", {})
                if today_str not in cache or not cache[today_str].get("total_sleep_seconds"):
                    print(f"[sleep-cron] Auto-syncing Oura for morning {today_str}...", flush=True)
                    sync_oura_cloud(gemini_key, token)
        except Exception as e:
            print(f"[sleep-cron] Auto sync error: {e}", flush=True)
        time.sleep(1800)  # Check every 30 minutes


def build_context(gemini_key: str, token: str) -> str:
    vault = load_vault(gemini_key)
    files = dict(vault.get("files", {}))
    state = sync_state_from_cloud(token)

    sections = []
    for rel, content in files.items():
        if rel in ["dashboard.html", "bot/.oura_credentials.json", "bot/.oura_tokens.json"]:
            continue
        sections.append(f"=== {rel} ===\n{content}")

    if state.get("medication_logs"):
        sections.append("=== Cloud Synced Recent Doses ===\n" + json.dumps(state.get("medication_logs"), indent=2))
    if state.get("notes"):
        sections.append("=== Cloud Synced Notes ===\n" + json.dumps(state.get("notes"), indent=2))
    if state.get("hunger_logs"):
        sections.append("=== Cloud Synced Night Hunger ===\n" + json.dumps(state.get("hunger_logs"), indent=2))
    if state.get("alcohol_logs"):
        sections.append("=== Cloud Synced Alcohol Logs ===\n" + json.dumps(state.get("alcohol_logs"), indent=2))
    if state.get("nicotine_logs"):
        sections.append("=== Cloud Synced Nicotine Logs ===\n" + json.dumps(state.get("nicotine_logs"), indent=2))

    return "\n\n".join(sections)


def call_gemini(gemini_key: str, prompt: str, system_inst: str) -> str:
    models = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
    payload = {
        "system_instruction": {"parts": [{"text": system_inst}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }
    for model in models:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with _open_url(req, timeout=25) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            parts = res.get("candidates", [])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            if text:
                return strip_markdown_asterisks(text)
        except Exception:
            continue
    return "Roger Roger. Processed your message."


def _parse_ampm_minutes(t_str: str) -> Optional[int]:
    if not t_str or t_str == "—":
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$", t_str, re.IGNORECASE)
    if not m:
        return None
    h = int(m.group(1))
    mins = int(m.group(2))
    ap = m.group(3).upper() if m.group(3) else None
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    return h * 60 + mins


def _calc_med_to_sleep_str(med_time_str: str, sleep_time_str: str) -> str:
    m1 = _parse_ampm_minutes(med_time_str)
    m2 = _parse_ampm_minutes(sleep_time_str)
    if m1 is None or m2 is None:
        return "—"
    if m1 < 720:
        m1 += 1440
    if m2 < 720:
        m2 += 1440
    diff = m2 - m1
    if diff < 0 or diff > 720:
        return "—"
    if diff >= 60:
        return f"{diff // 60}h {diff % 60:02d}m"
    return f"{diff}m"


def _js_round(val: float) -> int:
    """Match JavaScript Math.round() half-up rounding for non-negative scores."""
    return int(math.floor(float(val) + 0.5))


def _extract_sleep_features_py(oura: Dict[str, Any]) -> Dict[str, float]:
    tot_hrs = oura.get("total_sleep_hours")
    if tot_hrs is None:
        tot_hrs = round((oura.get("total_sleep_seconds") or 0) / 3600, 1)
    safe_hrs = max(1.0, float(tot_hrs or 0))
    eff = float(oura.get("sleep_efficiency") if oura.get("sleep_efficiency") is not None else 85)
    wakeups = float(oura.get("wakeups") if oura.get("wakeups") is not None else 0)
    awake_mins = float(oura.get("awake_mins") if oura.get("awake_mins") is not None else 0)
    deep = float(_js_round(oura.get("deep_sleep_pct") or 0))
    rem = float(_js_round(oura.get("rem_sleep_pct") or 0))
    hrv = float(oura.get("average_hrv") if oura.get("average_hrv") is not None else 50)
    low_hr = float(oura.get("lowest_heart_rate") if oura.get("lowest_heart_rate") is not None else 50)
    return {
        "hrs": float(tot_hrs or 0),
        "eff": eff,
        "wu_rate": wakeups / safe_hrs,
        "aw_rate": awake_mins / safe_hrs,
        "deep": deep,
        "rem": rem,
        "hrv": hrv,
        "low_hr": low_hr,
    }


def _calc_custom_scores_py(
    oura: Dict[str, Any],
    all_records: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Optional[int]]:
    if not oura:
        return {
            "total_weighted": None,
            "total_bottleneck": None,
            "total_personal": None,
            "continuity": None,
            "restoration": None,
        }

    f = _extract_sleep_features_py(oura)
    wu_pts = max(0.0, min(40.0, 40.0 - f["wu_rate"] * 28.0))
    aw_pts = max(0.0, min(40.0, 40.0 - max(0.0, f["aw_rate"] - 2.0) * 3.5))
    eff_pts = max(0.0, min(20.0, ((f["eff"] - 75.0) / 20.0) * 20.0))
    continuity_raw = wu_pts + aw_pts + eff_pts

    restoration_raw = (
        min(30.0, (f["deep"] / 20.0) * 30.0)
        + min(25.0, (f["rem"] / 20.0) * 25.0)
        + min(25.0, (f["hrv"] / 80.0) * 25.0)
        + max(0.0, min(20.0, (60.0 - f["low_hr"]) * 2.0))
    )

    p_qty = 0.6 * min(100.0, (f["hrs"] / 8.0) * 100.0) + 0.4 * max(0.0, min(100.0, ((f["eff"] - 75.0) / 20.0) * 100.0))
    p_cont = continuity_raw
    p_arch = 0.5 * min(100.0, (f["deep"] / 23.0) * 100.0) + 0.5 * min(100.0, (f["rem"] / 25.0) * 100.0)
    p_auto = 0.6 * min(100.0, (f["hrv"] / 95.0) * 100.0) + 0.4 * max(0.0, min(100.0, ((65.0 - f["low_hr"]) / 20.0) * 100.0))

    weighted_raw = (p_qty + p_cont + p_arch + p_auto) / 4.0
    geom = (max(1.0, p_qty) * max(1.0, p_cont) * max(1.0, p_arch) * max(1.0, p_auto)) ** 0.25
    bottleneck_raw = 0.75 * geom + 0.25 * min(p_qty, p_cont, p_arch, p_auto)

    personal_score = 75
    if all_records:
        feats_list = [
            _extract_sleep_features_py(rec)
            for d_k, rec in sorted(all_records.items())
            if d_k >= "2026-11-03" and rec and ((rec.get("total_sleep_seconds") or 0) > 0 or (rec.get("total_sleep_hours") or 0) > 0)
        ]
        if len(feats_list) >= 2:
            specs = [
                ("hrs", 1.0), ("eff", 1.0), ("wu_rate", -1.0), ("aw_rate", -1.0),
                ("deep", 1.0), ("rem", 1.0), ("hrv", 1.0), ("low_hr", -1.0),
            ]
            zs = []
            for key, sign in specs:
                vals = [item[key] for item in feats_list]
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / len(vals)
                sd = var ** 0.5
                z = ((f[key] - mean) / sd) * sign if sd > 0 else 0.0
                zs.append(max(-2.5, min(2.5, z)))
            personal_score = max(0, min(100, _js_round(75.0 + (sum(zs) / len(zs)) * 15.0)))

    return {
        "total_weighted": max(0, min(100, _js_round(weighted_raw))),
        "total_bottleneck": max(0, min(100, _js_round(bottleneck_raw))),
        "total_personal": personal_score,
        "continuity": max(0, min(100, _js_round(continuity_raw))),
        "restoration": max(0, min(100, _js_round(restoration_raw))),
    }


def format_sleep_status_reply(text: str, gemini_key: str, token: str) -> Optional[str]:
    """Handle 9, /sleep, /lastnight, 'sleep status', 'last night', or '/sleep MM-DD' queries."""
    t_clean = text.strip()
    t_low = t_clean.lower()

    is_nine = bool(re.match(r"^/?9(?:\.|$)", t_clean) or t_low in ["9", "9."])
    is_cmd = bool(re.match(r"^/(?:sleep|health|lastnight|status|score|today)\b", t_low))
    is_phrase = bool(
        re.match(r"^(?:(?:sleep|health)\s*status|last\s*night(?:\s+sleep)?|how\s+did\s+i\s+sleep(?:\s+(?:last\s+night|on\s+.+))?|(?:sleep|health)\s+report|status(?:\s+[\d/\-]+)?)$", t_low)
    )
    sleep_inquiry_patterns = [
        r"^/?9(?:\.|$)",
        r"^/(?:sleep|health|lastnight|status|score|today)\b",
        r"\bhow\s+(?:was|did)\s+(?:my\s+|the\s+)?sleep\b",
        r"\bhow\s+did\s+i\s+sleep\b",
        r"\bhow\s+was\s+(?:last\s+night|yesterday)\b",
        r"\b(?:sleep|health)\s*(?:stats|status|score|report)\b",
        r"\blast\s*night(?:\s+sleep)?\b",
        r"\b(?:i\s+want\s+)?all\s+(?:the\s+)?stats\b",
        r"\bwhat\s+(?:was|were)\s+(?:my\s+)?sleep\s+stats\b",
        r"\bgive\s+me\s+(?:all\s+)?(?:the\s+)?(?:sleep\s+)?stats\b",
        r"\bmy\s+sleep\b",
        r"\bsleep\s+score\b",
    ]
    matched = bool(is_nine or is_cmd or is_phrase or any(re.search(pat, t_low) for pat in sleep_inquiry_patterns))
    if not matched:
        return None

    now = now_cst()
    target_night_of = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    m_iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t_clean)
    m_md = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t_clean)
    if m_iso:
        target_night_of = m_iso.group(1)
    elif m_md:
        mo, dy = int(m_md.group(1)), int(m_md.group(2))
        yr_raw = m_md.group(3)
        yr = int(yr_raw) + 2000 if (yr_raw and len(yr_raw) == 2) else (int(yr_raw) if yr_raw else now.year)
        try:
            cand = datetime(yr, mo, dy)
            if not yr_raw and cand.date() > now.date():
                cand = datetime(yr - 1, mo, dy)
            target_night_of = cand.strftime("%Y-%m-%d")
        except ValueError:
            pass

    try:
        wakeup_date = (datetime.strptime(target_night_of, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return "Roger Roger.\n\nInvalid date format. Try /sleep or /sleep 11-12."

    vault = load_vault(gemini_key)
    files = vault.get("files", {})
    state = sync_state_from_cloud(token)

    def _collect_records():
        recs = {}
        try:
            recs.update(json.loads(files.get("data/oura_cache.json", "{}")).get("daily_records", {}))
        except Exception:
            pass
        recs.update((state.get("oura_cache") or {}).get("daily_records", {}))
        return recs

    daily_records = _collect_records()
    if wakeup_date not in daily_records and wakeup_date == now.strftime("%Y-%m-%d"):
        sync_oura_cloud(gemini_key, token)
        state = sync_state_from_cloud(token)
        daily_records = _collect_records()

    oura = daily_records.get(wakeup_date)
    # If today's wakeup record hasn't synced yet and no specific date was queried, gracefully use latest available
    if not oura and not m_iso and not m_md and daily_records:
        latest_avail_wake = sorted(daily_records.keys())[-1]
        oura = daily_records[latest_avail_wake]
        wakeup_date = latest_avail_wake
        target_night_of = (datetime.strptime(wakeup_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    # Merge medication logs
    med_by_night = {}
    try:
        for log in json.loads(files.get("data/medication_logs.json", "{}")).get("logs", []):
            if log.get("night_of"):
                med_by_night[log["night_of"]] = dict(log)
    except Exception:
        pass
    for log in state.get("medication_logs", []):
        if log.get("night_of"):
            med_by_night[log["night_of"]] = dict(log)

    med = med_by_night.get(target_night_of, {})
    hunger = (state.get("hunger_logs") or {}).get(target_night_of) or med.get("hunger_level") or "—"
    alcohol = (state.get("alcohol_logs") or {}).get(target_night_of) or med.get("alcohol") or "No"
    nicotine = (state.get("nicotine_logs") or {}).get(target_night_of) or med.get("nicotine") or "—"

    if not oura and not med:
        return f"Roger Roger.\n\nNo sleep or medication data found for night of {target_night_of}."

    def _fmt_iso(iso_s: Optional[str]) -> str:
        if not iso_s:
            return "—"
        try:
            return datetime.fromisoformat(iso_s).strftime("%I:%M %p").lstrip("0")
        except Exception:
            return "—"

    med_time = med.get("time_taken", "—")
    traz = f"{med.get('trazodone_mg')}mg" if med.get("trazodone_mg") is not None else "—"
    quet = f"{med.get('quetiapine_mg')}mg" if med.get("quetiapine_mg") is not None else "—"

    if not oura:
        return (
            f"Roger Roger.\n\n"
            f"Health Tracker Status — Night of {target_night_of} (Oura Pending)\n"
            f"- Meds: Traz {traz} / Quet {quet} @ {med_time}\n"
            f"- Hunger: {hunger} | Alcohol: {alcohol} | Nicotine: {nicotine}\n"
            f"- Oura data for {wakeup_date} is not synced yet."
        )

    b_start = oura.get("bedtime_start")
    b_end = oura.get("bedtime_end")
    lat_sec = oura.get("latency_seconds")
    lat_mins = oura.get("latency_mins")
    if lat_sec is None and lat_mins is not None:
        lat_sec = lat_mins * 60
    elif lat_sec is None:
        lat_sec = 0
        lat_mins = 0
    if lat_mins is None:
        lat_mins = round(lat_sec / 60)

    bedtime_str = _fmt_iso(b_start)
    wake_str = _fmt_iso(b_end)
    sleep_time_str = "—"
    if b_start:
        try:
            sleep_dt = datetime.fromisoformat(b_start) + timedelta(seconds=lat_sec)
            sleep_time_str = sleep_dt.strftime("%I:%M %p").lstrip("0")
        except Exception:
            pass

    med_to_sleep = _calc_med_to_sleep_str(med_time, sleep_time_str)
    custom = _calc_custom_scores_py(oura, daily_records)
    restfulness = (oura.get("sleep_contributors") or {}).get("restfulness")
    if restfulness is None:
        restfulness = oura.get("restfulness", "—")

    sleep_score = oura.get("sleep_score", "—")
    readiness_score = oura.get("readiness_score", "—")
    dur = oura.get("total_sleep_formatted", "—")
    eff = oura.get("sleep_efficiency", "—")
    deep_fmt = oura.get("deep_sleep_formatted") or (f"{round((oura.get('deep_sleep_seconds') or 0)/3600, 1)}h" if oura.get("deep_sleep_seconds") else "—")
    deep_pct = oura.get("deep_sleep_pct", "—")
    rem_fmt = oura.get("rem_sleep_formatted") or (f"{round((oura.get('rem_sleep_seconds') or 0)/3600, 1)}h" if oura.get("rem_sleep_seconds") else "—")
    rem_pct = oura.get("rem_sleep_pct", "—")
    awake_fmt = oura.get("awake_formatted") or (f"{oura.get('awake_mins')}m" if oura.get("awake_mins") is not None else "—")
    wakeups = oura.get("wakeups", "—")
    rhr = oura.get("resting_heart_rate", "—")
    low_hr = oura.get("lowest_heart_rate", "—")
    hrv = oura.get("average_hrv", "—")

    try:
        dt_night = datetime.strptime(target_night_of, "%Y-%m-%d")
        friendly_night = dt_night.strftime("%b %d").replace(" 0", " ")
    except Exception:
        friendly_night = target_night_of

    med_date_str = f"Taken {friendly_night} at {med_time}" if med_time != "—" else f"Night of {friendly_night}"
    med_dose_str = f"{traz} Trazodone + {quet} Quetiapine ({med_date_str})" if (traz != "—" or quet != "—") else "No medication logged"

    # If Aaron asked specifically for sleep stats / how was my sleep / all the stats, return the clean 9-bullet vertical breakdown
    is_sleep_specific = bool(re.search(r"\b(?:how\s+(?:was|did)\s+(?:my\s+|the\s+)?sleep|how\s+did\s+i\s+sleep|all\s+(?:the\s+)?stats|sleep\s+stats|my\s+sleep)\b", t_low))
    if is_sleep_specific:
        return (
            f"- Latest Sleep Score: {sleep_score} (Night of {friendly_night})\n"
            f"- Latest Readiness Score: {readiness_score}\n"
            f"- Total Sleep: {dur} (Efficiency: {eff}%)\n"
            f"- Deep Sleep: {deep_fmt} ({deep_pct}%)\n"
            f"- REM Sleep: {rem_fmt} ({rem_pct}%)\n"
            f"- Awake Time: {awake_fmt} across {wakeups} awakenings\n"
            f"- Resting Heart Rate: {rhr} bpm (Lowest HR: {low_hr} bpm)\n"
            f"- Average HRV: {hrv} ms\n"
            f"- Last Medication Dosage: {med_dose_str}"
        )

    return (
        f"Roger Roger.\n\n"
        f"Health Tracker Status — Night of {target_night_of} (Wake {wakeup_date})\n"
        f"- Total Scores: Oura {oura.get('sleep_score', '—')} | Weighted {custom['total_weighted'] if custom['total_weighted'] is not None else '—'} | Bottleneck {custom['total_bottleneck'] if custom['total_bottleneck'] is not None else '—'} | Personal {custom['total_personal'] if custom['total_personal'] is not None else '—'}\n"
        f"- Sub-Scores: Continuity {custom['continuity'] if custom['continuity'] is not None else '—'} | Restoration {custom['restoration'] if custom['restoration'] is not None else '—'} | Restfulness {restfulness} | Efficiency {oura.get('sleep_efficiency', '—')}%\n"
        f"- Sleep: {oura.get('total_sleep_formatted', '—')} | Wakeups: {oura.get('wakeups', '—')} | Awake: {oura.get('awake_mins', '—')}m\n"
        f"- Timing: Med {med_time} -> Bed {bedtime_str} -> Asleep {sleep_time_str} -> Wake {wake_str}\n"
        f"- Latency: {lat_mins}m | Med-to-Sleep: {med_to_sleep}\n"
        f"- Recovery: Deep {round(oura.get('deep_sleep_pct') or 0)}% | REM {round(oura.get('rem_sleep_pct') or 0)}% | Avg HR {oura.get('average_heart_rate', '—')} | Low HR {oura.get('lowest_heart_rate', '—')} | HRV {oura.get('average_hrv', '—')}ms\n"
        f"- Inputs: Traz {traz} / Quet {quet} | Hunger: {hunger} | Alcohol: {alcohol} | Nicotine: {nicotine}"
    )


def _handle_sleep_batch(gemini_key: str, token: str, chat_id: int):
    with SLEEP_BATCH_LOCK:
        item = SLEEP_BATCH.pop(chat_id, None)
    if not item:
        return

    text = item.get("text", "").strip()
    text_lower = text.lower()

    if text_lower in ["ok", "okay", "roger"]:
        send_message(token, chat_id, "Roger Roger.")
        return

    if text_lower in ["/sync", "sync", "sync oura", "/sync oura"]:
        send_message(token, chat_id, "Syncing with Oura Ring...")
        reply = sync_oura_cloud(gemini_key, token)
        send_message(token, chat_id, reply)
        return

    status_reply = format_sleep_status_reply(text, gemini_key, token)
    if status_reply:
        send_message(token, chat_id, status_reply)
        return

    dose_reply = parse_and_record_dose(text, gemini_key, token)
    if dose_reply:
        send_message(token, chat_id, dose_reply)
        return

    if text_lower.startswith("idea dump") or "idea dump" in text_lower:
        state = sync_state_from_cloud(token)
        state.setdefault("notes", []).append({
            "timestamp": now_cst().isoformat(),
            "note": text
        })
        save_state_to_cloud(token, state)
        send_message(token, chat_id, "Roger Roger.\n\nUpdate Summary:\n- Logged idea dump note to cloud memory.")
        return

    if "hunger" in text_lower or "alcohol" in text_lower or "nicotine" in text_lower:
        state = sync_state_from_cloud(token)
        now = now_cst()
        night_of_key = (now - timedelta(days=1)).strftime("%Y-%m-%d") if now.hour < 14 else now.strftime("%Y-%m-%d")
        logged_msgs = []

        if "hunger" in text_lower:
            level = "None"
            if "high" in text_lower:
                level = "High"
            elif "med" in text_lower:
                level = "Medium"
            elif "low" in text_lower:
                level = "Low"
            elif "none" in text_lower or "non" in text_lower or "zero" in text_lower or "no hunger" in text_lower:
                level = "None"
            state.setdefault("hunger_logs", {})[night_of_key] = level
            for m in state.get("medication_logs", []):
                if m.get("night_of") == night_of_key:
                    m["hunger_level"] = level
            logged_msgs.append(f"Logged Night Hunger: {level}")

        if "alcohol" in text_lower:
            alc_val = "No" if re.search(r"\b(?:no|none|0|false)\b", text_lower) else "Yes"
            state.setdefault("alcohol_logs", {})[night_of_key] = alc_val
            for m in state.get("medication_logs", []):
                if m.get("night_of") == night_of_key:
                    m["alcohol"] = alc_val
            logged_msgs.append(f"Logged Alcohol ({night_of_key}): {alc_val}")

        if "nicotine" in text_lower:
            if "unknown" in text_lower:
                nic_val = "Unknown"
            elif re.search(r"\b(?:no|none|0|false)\b", text_lower):
                nic_val = "No"
            else:
                nic_val = "Yes"
            state.setdefault("nicotine_logs", {})[night_of_key] = nic_val
            for m in state.get("medication_logs", []):
                if m.get("night_of") == night_of_key:
                    m["nicotine"] = nic_val
            logged_msgs.append(f"Logged Nicotine ({night_of_key}): {nic_val}")

        save_state_to_cloud(token, state)
        send_message(token, chat_id, "Roger Roger.\n\n" + "\n".join(logged_msgs))
        return

    # Question or conversation turn
    system_inst = (
        "You are Aaron's personal Health Tracker (Sleep & Biometrics) AI Assistant. "
        "Strict rules:\n"
        "1. Be as short, direct, and scannable as humanly possible.\n"
        "2. Use brief bullet points with dashes (-). Zero fluff.\n"
        "3. NEVER use markdown asterisks (* or **). No bolding asterisks.\n"
        "4. Focus exclusively on health & sleep tracking, Trazodone & Quetiapine dosages, awakenings, and Oura / biometric data.\n"
        "5. Affirmations are always 'Roger Roger.'\n"
        "6. MANDATORY INTERNAL 'WHY' VERIFICATION (SILENT IN CHAT): Before finalizing any sleep/health insight, dosage correlation, or metric interpretation, internally verify WHY that conclusion is supported by exact records in medication_logs and oura_cache. Reject unverified guesses and keep internal 'Why' reasoning silent in chat unless Aaron asks for it.\n"
        "7. FULL SLEEP STATS STANDARD: Whenever Aaron asks how he slept, how last night was, or asks for sleep stats/scores, ALWAYS provide the complete breakdown across all 9 metrics:\n"
        "- Latest Sleep Score (with Readiness)\n"
        "- Total Sleep (with Sleep Efficiency %)\n"
        "- Deep Sleep (duration & %)\n"
        "- REM Sleep (duration & %)\n"
        "- Awake Time (duration & awakenings)\n"
        "- Resting Heart Rate & Lowest HR\n"
        "- Average HRV\n"
        "- Last Medication Dosage (with time taken and night of)\n"
        "Never truncate or provide an incomplete subset of metrics.\n"
        "8. Alcohol automatically defaults to 'No' every night unless Aaron explicitly says otherwise.\n"
        "9. Custom Sleep Scores use two tiers: 3 Total Sleep Score formulations (Weighted, Bottleneck, Personal Baseline alongside Oura Sleep Score) plus specific sub-scores (Continuity, Restoration, Restfulness).\n\n"
        + build_context(gemini_key, token)
    )
    reply = call_gemini(gemini_key, text, system_inst)
    send_message(token, chat_id, reply)


def _enqueue_sleep(gemini_key: str, token: str, chat_id: int, text: str):
    with SLEEP_BATCH_LOCK:
        existing = SLEEP_BATCH.get(chat_id)
        if existing:
            combined = existing["text"] + "\n" + text if text else existing["text"]
            existing["text"] = combined
            if existing.get("timer"):
                existing["timer"].cancel()
        else:
            SLEEP_BATCH[chat_id] = {"text": text}

        timer = threading.Timer(SLEEP_BATCH_DELAY, _handle_sleep_batch, args=(gemini_key, token, chat_id))
        SLEEP_BATCH[chat_id]["timer"] = timer
        timer.daemon = True
        timer.start()


def process_sleep_webhook(gemini_key: str, body_bytes: bytes):
    token = get_sleep_token(gemini_key)
    if not token:
        return
    try:
        upd = json.loads(body_bytes.decode("utf-8"))
        uid = upd.get("update_id")
        if uid:
            if uid in SLEEP_SEEN_UPDATES:
                return
            SLEEP_SEEN_UPDATES.add(uid)
        if "message" in upd:
            msg = upd["message"]
            cid = msg["chat"]["id"]
            if int(cid) not in ALLOWED_CHAT_IDS:
                return
            if "text" in msg:
                _enqueue_sleep(gemini_key, token, cid, text=msg["text"])
    except Exception as e:
        print(f"[sleep] Webhook error: {e}", flush=True)


def register_sleep_webhook(gemini_key: str, external_url: str):
    token = get_sleep_token(gemini_key)
    if not token or not external_url:
        return
    webhook_url = f"{external_url.rstrip('/')}/sleep-webhook"
    try:
        tg_api(token, "setWebhook", {"url": webhook_url})
        sync_state_from_cloud(token)
        print(f"[sleep] Webhook registered at {webhook_url}", flush=True)
        # Start morning auto-sync background thread
        t = threading.Thread(target=_auto_sync_loop, args=(gemini_key, token), daemon=True)
        t.start()
    except Exception as e:
        print(f"[sleep] Webhook setup failed: {e}", flush=True)


def get_dashboard_html(gemini_key: str) -> str:
    vault = load_vault(gemini_key)
    token = get_sleep_token(gemini_key)
    state = sync_state_from_cloud(token)
    files: Dict[str, str] = vault.get("files", {})
    html = files.get("dashboard.html", "<h1>Health Tracker Dashboard not found in vault</h1>")
    hunger_map = state.get("hunger_logs", {})
    alcohol_map = state.get("alcohol_logs", {})
    nicotine_map = state.get("nicotine_logs", {})
    inject_parts = []
    if hunger_map:
        inject_parts.append(f"const CLOUD_HUNGER = {json.dumps(hunger_map)};")
    if alcohol_map:
        inject_parts.append(f"const CLOUD_ALCOHOL = {json.dumps(alcohol_map)};")
    if nicotine_map:
        inject_parts.append(f"const CLOUD_NICOTINE = {json.dumps(nicotine_map)};")
    if state.get("medication_logs"):
        inject_parts.append(f"const CLOUD_DOSES = {json.dumps(state['medication_logs'])};")
    if state.get("oura_cache"):
        inject_parts.append(f"const CLOUD_OURA = {json.dumps(state['oura_cache'])};")
    if inject_parts:
        inject_js = "<script>" + " ".join(inject_parts) + "</script>"
        html = html.replace("<script>", f"{inject_js}\n  <script>", 1)
    return html


def get_live_sleep_json(gemini_key: str) -> str:
    vault = load_vault(gemini_key)
    token = get_sleep_token(gemini_key)
    files: Dict[str, str] = dict(vault.get("files", {}))
    state = sync_state_from_cloud(token)
    raw_meds = files.get("data/medication_logs.json", "[]")
    raw_oura = files.get("data/oura_cache.json", "{}")
    try:
        med_dict = json.loads(raw_meds)
        meds = list(med_dict.get("logs", [])) if isinstance(med_dict, dict) else list(med_dict)
        for dose in state.get("medication_logs", []):
            if not isinstance(dose, dict):
                continue
            d_date = dose.get("date")
            d_night = dose.get("night_of")
            existing = next(
                (l for l in meds if (d_date and l.get("date") == d_date) or (d_night and l.get("night_of") == d_night)),
                None,
            )
            if existing is None:
                meds.append(dose)
            else:
                existing.update({k: v for k, v in dose.items() if v is not None})
        for m in meds:
            if isinstance(m, dict) and not m.get("alcohol"):
                m["alcohol"] = "No"
        oura = state.get("oura_cache") or json.loads(raw_oura)
        return json.dumps(
            {
                "medication_logs": meds,
                "oura_cache": oura,
                "hunger_logs": state.get("hunger_logs", {}),
                "alcohol_logs": state.get("alcohol_logs", {}),
                "nicotine_logs": state.get("nicotine_logs", {}),
            },
            indent=2,
        )
    except Exception:
        return raw_meds

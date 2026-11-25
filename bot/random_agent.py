import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import ssl
import urllib.request
import zlib
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED_CHAT_IDS = {8410786728}
SEEN_UPDATES = set()
VAULT_CACHE: Dict[str, Any] = {}
CHAT_HISTORY: Dict[int, List[Dict[str, Any]]] = {}
_SSL_CTX = ssl.create_default_context()


def _open_url(req, timeout=20):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)


def now_cst() -> datetime:
    return datetime.now(timezone(timedelta(hours=-6)))


def strip_markdown_asterisks(text: str) -> str:
    return (text or "").replace("**", "").replace("*", "")


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def decrypt_vault(enc: dict, secret: str) -> dict:
    salt = base64.b64decode(enc["salt"])
    nonce = base64.b64decode(enc["nonce"])
    ct = base64.b64decode(enc["ct"])
    tag = base64.b64decode(enc["tag"])
    derived = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100000, dklen=64)
    enc_key, mac_key = derived[:32], derived[32:]
    expected = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("Invalid vault MAC")
    stream = _keystream(enc_key, nonce, len(ct))
    compressed = bytes(a ^ b for a, b in zip(ct, stream))
    raw = zlib.decompress(compressed)
    return json.loads(raw.decode("utf-8"))


def load_vault(gemini_key: str) -> dict:
    global VAULT_CACHE
    if VAULT_CACHE:
        return VAULT_CACHE
    vault_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "random_vault.json")
    if not os.path.exists(vault_path):
        vault_path = os.path.join(BASE_DIR, "bot", "random_vault.json")
    if not os.path.exists(vault_path):
        return {}
    with open(vault_path, "r", encoding="utf-8") as f:
        enc = json.load(f)
    VAULT_CACHE = decrypt_vault(enc, gemini_key)
    return VAULT_CACHE


def tg_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with _open_url(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(token: str, chat_id: int, text: str):
    clean = strip_markdown_asterisks(text).strip() or "Roger Roger."
    for i in range(0, len(clean), 3800):
        tg_api(token, "sendMessage", {"chat_id": chat_id, "text": clean[i:i + 3800]})


def sync_state_from_cloud(token: str) -> dict:
    try:
        res = tg_api(token, "getMyCommands", {"scope": {"type": "all_chat_administrators"}, "language_code": "eo"})
        cmds = res.get("result", [])
        sorted_cmds = sorted(
            [c for c in cmds if str(c.get("command", "")).startswith("r")],
            key=lambda x: int(str(x["command"])[1:] or "0"),
        )
        b64_str = "".join(str(c.get("description", "")).strip() for c in sorted_cmds)
        if not b64_str:
            return {"backlog": [], "errors": []}
        raw_bytes = base64.b64decode(b64_str.encode("ascii"))
        try:
            json_bytes = zlib.decompress(raw_bytes)
        except Exception:
            json_bytes = raw_bytes
        data = json.loads(json_bytes.decode("utf-8"))
        if isinstance(data, dict):
            data.setdefault("backlog", [])
            data.setdefault("errors", [])
            return data
    except Exception:
        pass
    return {"backlog": [], "errors": []}


def save_state_to_cloud(token: str, state: dict):
    try:
        # Preserve existing commands from other scopes/prefixes by keeping non-'r' commands in eo scope
        existing = tg_api(token, "getMyCommands", {"scope": {"type": "all_chat_administrators"}, "language_code": "eo"}).get("result", [])
        other_cmds = [c for c in existing if not str(c.get("command", "")).startswith("r")]

        raw_bytes = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        compressed = zlib.compress(raw_bytes, level=9)
        b64_str = base64.b64encode(compressed).decode("ascii")
        chunk_size = 250
        chunks = [b64_str[i:i + chunk_size] for i in range(0, len(b64_str), chunk_size)]
        r_cmds = [{"command": f"r{i}", "description": c} for i, c in enumerate(chunks[:90])]
        tg_api(token, "setMyCommands", {
            "commands": other_cmds + r_cmds,
            "scope": {"type": "all_chat_administrators"},
            "language_code": "eo",
        })
    except Exception:
        pass


def call_gemini(gemini_key: str, chat_id: int, user_text: str, files: dict) -> str:
    ctx_parts = []
    for rel in (
        "README.md",
        "rules/MasterRules.md",
        "memory/ProjectMemory.md",
        "memory/IdeaBacklog.md",
        "memory/Staging.md",
        "memory/ErrorLog.md",
    ):
        if files.get(rel):
            ctx_parts.append(f"=== {rel} ===\n{files[rel][:10000]}")

    system_inst = (
        "You are Aaron's random-fun / Dave Master Hub Telegram Assistant.\n"
        "Strict rules:\n"
        "- Never use markdown asterisks (* or **). Use dashes (-) and clean plain text.\n"
        "- Keep replies short, direct, and scannable.\n"
        "- Affirmations are always 'Roger Roger.'\n"
        "- Aaron can text 0 (menu), 1 (backlog), 2 (staging), 5 (error log), 6 (status), 7 (action log), 'Backlog: ...', or 'Error log: ...'.\n\n"
        + "\n\n".join(ctx_parts)
    )

    hist = CHAT_HISTORY.setdefault(chat_id, [])
    hist.append({"role": "user", "parts": [{"text": user_text}]})
    if len(hist) > 10:
        hist = hist[-10:]
        CHAT_HISTORY[chat_id] = hist

    payload = json.dumps({
        "contents": hist,
        "system_instruction": {"parts": [{"text": system_inst}]},
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1536},
    }).encode("utf-8")

    last_err = ""
    for model in ("gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with _open_url(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            if text:
                reply = strip_markdown_asterisks(text).strip()
                hist.append({"role": "model", "parts": [{"text": reply}]})
                return reply
        except Exception as e:
            last_err = str(e)
            continue
    return f"Error reaching Gemini API ({last_err or 'all models failed'}). Try again."


def register_random_webhook(gemini_key: str, external_url: str):
    if not external_url:
        return
    vault = load_vault(gemini_key)
    token = vault.get("bot_token")
    if not token:
        print("[random-agent] No bot_token in random_vault.json")
        return
    webhook_url = f"{external_url.rstrip('/')}/random-webhook"
    res = tg_api(token, "setWebhook", {"url": webhook_url})
    print(f"[random-agent] Webhook registered at {webhook_url}: {res.get('ok')}")


def process_random_webhook(gemini_key: str, body: bytes):
    try:
        upd = json.loads(body.decode("utf-8"))
        uid = upd.get("update_id")
        if uid is not None:
            if uid in SEEN_UPDATES:
                return
            SEEN_UPDATES.add(uid)
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id or int(chat_id) not in ALLOWED_CHAT_IDS:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text and msg.get("photo"):
            text = "Uploaded screenshot (no caption)"
        if not text:
            return

        vault = load_vault(gemini_key)
        token = vault.get("bot_token")
        if not token:
            return
        files = vault.get("files", {})
        t_low = text.lower()
        cid = int(chat_id)

        if t_low in ("0", "actions", "list actions", "/actions"):
            reply = (
                "Roger Roger.\n\n"
                "Random-Fun Action Menu:\n"
                "- 0: Action menu\n"
                "- 1: Pull backlog\n"
                "- 2: Pull staging\n"
                "- 5: Pull error log\n"
                "- 6: Status\n"
                "- 7: Pull action log\n"
                "- 8: Pull change log\n"
                "- Backlog: <idea>\n"
                "- Error log: <note>"
            )
        elif t_low in ("1", "backlog", "pull backlog"):
            cloud_b = sync_state_from_cloud(token).get("backlog", [])
            base_b = files.get("memory/IdeaBacklog.md", "(empty)")
            extra = "\n".join(f"- [{b.get('ts', '')} Cloud] {b.get('text', '')}" for b in cloud_b)
            reply = f"{base_b}\n{extra}".strip()
        elif t_low in ("2", "staging", "pull staging"):
            reply = files.get("memory/Staging.md", "(Staging empty)")
        elif t_low in ("5", "error log", "pull error log"):
            cloud_e = sync_state_from_cloud(token).get("errors", [])
            base_e = files.get("memory/ErrorLog.md", "(empty)")
            extra = "\n".join(f"- [{e.get('ts', '')} Cloud] {e.get('text', '')}" for e in cloud_e)
            reply = f"{base_e}\n{extra}".strip()
        elif t_low in ("6", "status"):
            reply = (
                "Roger Roger.\n\n"
                "- Project: random-fun (Render 24/7 webhook active)\n"
                "- Hub: dashboard.html, interviews.html, flashcards.html, architecture.html\n"
                "- Last vault sync: see ActionLog"
            )
        elif t_low in ("7", "action log", "pull action log"):
            lines = files.get("memory/ActionLog.md", "").splitlines()
            reply = "\n".join(lines[-15:]) if lines else "(empty)"
        elif t_low in ("8", "change log", "changelog", "pull change log"):
            lines = [ln for ln in files.get("memory/ActionLog.md", "").splitlines() if "| Change |" in ln]
            reply = "\n".join(lines[-15:]) if lines else "(empty)"
        elif re.match(r"^(?:backlog|idea\s*dump|idea)\s*[:\-]?\s+", t_low):
            note = re.sub(r"^(?:backlog|idea\s*dump|idea)\s*[:\-]?\s*", "", text, flags=re.I).strip()
            st = sync_state_from_cloud(token)
            st.setdefault("backlog", []).append({"ts": now_cst().strftime("%Y-%m-%d %H:%M"), "text": note})
            save_state_to_cloud(token, st)
            reply = f"Roger Roger.\n\nAdded to random-fun Backlog:\n- {note}"
        elif re.match(r"^error\s*log\s*[:\-]?\s+", t_low):
            note = re.sub(r"^error\s*log\s*[:\-]?\s*", "", text, flags=re.I).strip()
            st = sync_state_from_cloud(token)
            st.setdefault("errors", []).append({"ts": now_cst().strftime("%Y-%m-%d %H:%M"), "text": note})
            save_state_to_cloud(token, st)
            reply = f"Roger Roger.\n\n- Logged to ErrorLog.md: {note}"
        else:
            reply = call_gemini(gemini_key, cid, text, files)

        send_message(token, cid, reply)
    except Exception as e:
        print(f"[random-agent] Webhook error: {e}")

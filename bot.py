# ============================================================
# VENU AI — MERGED SINGLE-FILE TELEGRAM BOT
# ============================================================
# Features merged into one file:
# - OpenAI-compatible AI API / Claude Sonnet 4.6
# - Supabase persistent users, profiles, messages and daily stats
# - TTL memory/cache + duplicate-response protection
# - Guess Number / Truth or Dare / Riddle / Roast Battle
# - Roast Mode / Bakchodi Mode
# - Calculator with safe AST evaluation
# - Reply keyboard, Add-to-Group, Profile, Clear Chat
# - Help, Stats, Memory viewer, Joke, Shayari, Fun Zone, Dice, Coin, Choose
# - Better conversational prompting and natural follow-up chat
# - Private-chat + group mention/reply filtering
# - Voice input -> speech recognition -> AI -> optional TTS reply
# - Admin broadcast / refresh
# - Flask keep-alive endpoint
# - Retry/backoff logging
#
# IMPORTANT:
# Put secrets in environment variables:
# BOT_TOKEN
# AI_API_KEY
# AI_BASE_URL
# AI_MODEL
# SUPABASE_URL
# SUPABASE_KEY
#
# Do NOT hard-code Telegram/AI/Supabase secrets in source code.
# ============================================================

from collections import deque
from difflib import SequenceMatcher
import ast
import json
import logging
from logging.handlers import RotatingFileHandler
import operator
import os
import random
import re
import tempfile
import threading
import time

from cachetools import TTLCache
from flask import Flask
from gtts import gTTS
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pydub import AudioSegment
import speech_recognition as sr
import telebot
from telebot import types


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

def env_int(name, default=0):
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger = logging.getLogger(__name__)
        logger.warning("Invalid integer in %s; using %s", name, default)
        return default

ADMIN_ID = env_int("ADMIN_ID", 0)
BOT_USERNAME = ""
AI_BASE_URL = os.getenv("AI_BASE_URL", "").strip().rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "").strip()

PORT = int(os.getenv("PORT", "10000"))
ENABLE_TTS = os.getenv("ENABLE_TTS", "true").lower() in {"1", "true", "yes", "on"}
RESPOND_IN_GROUPS = os.getenv("RESPOND_IN_GROUPS", "true").lower() in {"1", "true", "yes", "on"}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY environment variable is missing.")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables are required.")
if not AI_BASE_URL:
    raise RuntimeError("AI_BASE_URL environment variable is missing.")
if not AI_MODEL:
    raise RuntimeError("AI_MODEL environment variable is missing.")


# ============================================================
# LOGGING
# ============================================================

handler = RotatingFileHandler(
    "bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler, logging.StreamHandler()],
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

try:
    BOT_INFO = bot.get_me()
    BOT_ID = BOT_INFO.id
    BOT_USERNAME = (BOT_INFO.username or "").lower()
except Exception:
    logger.exception("Could not fetch Telegram bot information during startup")
    BOT_ID = None


# ============================================================
# SUPABASE CLIENT
# ============================================================

class SupabaseClient:
    def __init__(self, url, key):
        self.url = url
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

        self.session = requests.Session()

        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=retry,
        )

        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def request(self, method, endpoint, payload=None, params=None):
        target_url = f"{self.url}/rest/v1/{endpoint}"

        try:
            method = method.upper()

            if method == "GET":
                response = self.session.get(
                    target_url,
                    headers=self.headers,
                    params=params,
                    timeout=12,
                )
            elif method == "POST":
                response = self.session.post(
                    target_url,
                    headers=self.headers,
                    json=payload,
                    timeout=12,
                )
            elif method == "PATCH":
                response = self.session.patch(
                    target_url,
                    headers=self.headers,
                    json=payload,
                    timeout=12,
                )
            elif method == "DELETE":
                response = self.session.delete(
                    target_url,
                    headers=self.headers,
                    timeout=12,
                )
            else:
                logger.error("Unsupported Supabase method: %s", method)
                return None

            response.raise_for_status()

            if response.text:
                return response.json()

            return None

        except Exception:
            logger.exception("Supabase request error [%s %s]", method, endpoint)
            return None


db = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# GLOBAL STATE / CACHES
# ============================================================

state_lock = threading.RLock()

user_memory_cache = TTLCache(maxsize=1500, ttl=5400)
registered_users_cache = TTLCache(maxsize=5000, ttl=86400)

last_message_time = {}
user_name_mention_time = {}
user_recent_replies = {}
ACTIVE_GAME_SESSIONS = {}
TTS_USERS = set()
USER_ACTIVITY = {}


# ============================================================
# FLASK KEEP-ALIVE
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return (
        "🤖 Venu AI is online — AI, Supabase Memory, Games, "
        "Roast Mode, Group Support, Voice/TTS."
    )


@app.route("/health")
def health():
    return {
        "status": "online",
        "bot_id": BOT_ID,
        "model": AI_MODEL,
    }


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )


# ============================================================
# PROFILE / MEMORY
# ============================================================
def default_profile(user_id, name="Dost"):
    return {
        "user_id": user_id,
        "name": name or "Dost",
        "age": "Not specified",
        "favorite_game": "Not specified",
        "favorite_movie": "Not specified",
        "language": "Hinglish",
        "relationship_status": "Not specified",
        "hobbies": "Not specified",
        "current_mood": "Witty, loyal, and consistently chill",
        "emotional_momentum": "Stable",
    }


def register_user(user_id, username, first_name):
    with state_lock:
        if user_id in registered_users_cache:
            return

    payload = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "is_verified": True,
    }

    headers = {
        **db.headers,
        "Prefer": "resolution=merge-duplicates",
    }

    try:
        response = db.session.post(
            f"{db.url}/rest/v1/users",
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()

        with state_lock:
            registered_users_cache[user_id] = True

    except Exception:
        logger.exception("Error registering user")


def clear_user_memory(user_id):
    with state_lock:
        db.request("DELETE", f"messages?user_id=eq.{user_id}")

        user_memory_cache.pop(user_id, None)

        if user_id in user_recent_replies:
            user_recent_replies[user_id].clear()

        last_message_time.pop(user_id, None)
        ACTIVE_GAME_SESSIONS.pop(user_id, None)
        TTS_USERS.discard(user_id)


def get_user_memory(user_id, first_name="Dost"):
    with state_lock:
        if user_id in user_memory_cache:
            return user_memory_cache[user_id]

    rows = db.request(
        "GET",
        f"user_profiles?user_id=eq.{user_id}",
    )

    if rows:
        profile = rows[0]
    else:
        profile = default_profile(user_id, first_name)
        db.request(
            "POST",
            "user_profiles",
            payload=profile,
        )

    summary_rows = db.request(
        "GET",
        f"conversation_summary?user_id=eq.{user_id}",
    )

    summary = (
        summary_rows[0].get("summary", "Ongoing friendly connection.")
        if summary_rows
        else "Ongoing friendly connection."
    )

    message_rows = db.request(
        "GET",
        f"messages?user_id=eq.{user_id}&order=created_at.desc&limit=20",
    )

    history = (
        [
            {
                "role": row["role"],
                "content": row["content"],
            }
            for row in reversed(message_rows)
        ]
        if message_rows
        else []
    )

    memory_packet = {
        "profile": profile,
        "summary": summary,
        "history": history,
    }

    with state_lock:
        user_memory_cache[user_id] = memory_packet

    return memory_packet


def update_profile_field(user_id, field, value):
    allowed_fields = {
    "name",
    "age",
    "favorite_game",
    "favorite_movie",
    "language",
    "relationship_status",
    "hobbies",
    "current_mood",
        "emotional_momentum",
    }

    if field not in allowed_fields:
        return

    db.request(
        "PATCH",
        f"user_profiles?user_id=eq.{user_id}",
        payload={field: value},
    )

    with state_lock:
        if user_id in user_memory_cache:
            user_memory_cache[user_id]["profile"][field] = value


def save_message(user_id, role, content):
    if not content:
        return

    ignored = {
        "hi",
        "hello",
        "ok",
        "hmm",
        "k",
        "acha",
        "hlo",
    }

    if role == "user" and content.lower().strip() in ignored:
        return

    db.request(
        "POST",
        "messages",
        payload={
            "user_id": user_id,
            "role": role,
            "content": content,
        },
    )

    with state_lock:
        if user_id in user_memory_cache:
            history = user_memory_cache[user_id]["history"]
            history.append(
                {
                    "role": role,
                    "content": content,
                }
            )

            if len(history) > 20:
                history.pop(0)


def increment_daily_stats(user_id, is_game=False):
    try:
        date_str = time.strftime("%Y-%m-%d")

        existing = db.request(
            "GET",
            f"daily_stats?user_id=eq.{user_id}&date=eq.{date_str}",
        )

        if existing:
            messages_sent = existing[0].get("messages_sent", 0)
            games_played = existing[0].get("games_played", 0)

            messages_sent += 0 if is_game else 1
            games_played += 1 if is_game else 0

            db.request(
                "PATCH",
                f"daily_stats?user_id=eq.{user_id}&date=eq.{date_str}",
                payload={
                    "messages_sent": messages_sent,
                    "games_played": games_played,
                },
            )
        else:
            db.request(
                "POST",
                "daily_stats",
                payload={
                    "user_id": user_id,
                    "date": date_str,
                    "messages_sent": 0 if is_game else 1,
                    "games_played": 1 if is_game else 0,
                },
            )

    except Exception:
        logger.exception("Daily stats update error")


# ============================================================
# SAFE CALCULATOR
# ============================================================

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value

    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPERATORS:
        left = safe_eval(node.left)
        right = safe_eval(node.right)
        return SAFE_OPERATORS[type(node.op)](left, right)

    if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPERATORS:
        operand = safe_eval(node.operand)
        return SAFE_OPERATORS[type(node.op)](operand)

    raise ValueError("Unsafe math expression.")


def evaluate_math(expression):
    try:
        expression = expression.strip()

        if len(expression) > 100:
            return None

        node = ast.parse(expression, mode="eval")
        return safe_eval(node.body)

    except Exception:
        return None



def check_similarity(new_text, previous_texts, threshold=0.75):
    for previous in previous_texts:
        if SequenceMatcher(
            None,
            new_text.lower(),
            previous.lower(),
        ).ratio() >= threshold:
            return True

    return False


def clean_json_response(content):
    content = content.strip()

    if content.startswith("```"):
        parts = content.split("```")

        if len(parts) >= 2:
            content = parts[1].strip()

        if content.lower().startswith("json"):
            content = content[4:].strip()

    return content


def generate_unified_ai_response(
    user_id,
    memory_packet,
    latest_user_text,
):
    profile = memory_packet["profile"]
    summary = memory_packet["summary"]
    history = memory_packet["history"]

    with state_lock:
        if user_id not in user_recent_replies:
            user_recent_replies[user_id] = deque(maxlen=10)

    mood_desc = (
    "Witty, loyal, emotionally intelligent, supportive and chill"
)

    system_prompt = f"""
You are Venu, a consistent desi best friend.

PERSONA:
- Natural Hinglish.
- Witty, street-smart and funny.
- Loyal and supportive when the user is serious.
- Funny and playful, but never intentionally abusive or hostile.
- Never become randomly rude in a serious/emotional situation.
- Do not claim to be a human.
- Keep continuity with prior conversation.

CURRENT VIBE:
{mood_desc}

IMPORTANT:
Return STRICT JSON only with exactly two top-level keys:
{{
  "classification": {{
    "mood": "...",
    "intent": "chat|game|help|calculator"
  }},
  "reply": "..."
}}

Reply must be SHORT: usually 1 sentence, maximum 2 short sentences in Hinglish.
- Do not over-explain.
- Simple questions should get a short answer.
- Do not use the user name in every reply; use it only sometimes.

USER PROFILE:
Name: {profile.get("name")}
Favorite Game: {profile.get("favorite_game")}
Favorite Movie: {profile.get("favorite_movie")}
Language: {profile.get("language")}
Current Mood: {profile.get("current_mood")}

ONGOING SUMMARY:
{summary}
""".strip()

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    for msg in history[-20:]:
        role = msg.get("role")
        content = msg.get("content")

        if role in {"user", "assistant"} and content:
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": latest_user_text,
        }
    )

    url = f"{AI_BASE_URL}/chat/completions"

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(3):
        try:
            payload = {
                "model": AI_MODEL,
                "messages": messages,
                "temperature": 0.75 + (attempt * 0.05),
                "max_tokens": 140,
            }

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=25,
            )

            response.raise_for_status()

            data = response.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                logger.error("AI provider returned unexpected response: %s", data)
                raise ValueError("AI provider returned an unexpected response format.") from exc

            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in content
                )

            content = clean_json_response(str(content))

            parsed = json.loads(content)

            reply = str(parsed.get("reply", "")).strip()

            classification = parsed.get(
                "classification",
                {
                    "mood": "Witty and Supportive",
                    "intent": "chat",
                },
            )

            if not reply:
                raise ValueError("AI provider returned an empty reply.")

            with state_lock:
                if not check_similarity(
                    reply,
                    user_recent_replies[user_id],
                    threshold=0.75,
                ):
                    user_recent_replies[user_id].append(reply)
                    return classification, reply

        except Exception:
            logger.exception(
                "AI unified API exception on attempt %s",
                attempt + 1,
            )

    fallback = (
        "Arey yaar, connection thoda slow ho gaya tha 😭 "
        "par main yahin hoon. Bata kya scene hai? 🤭"
    )

    with state_lock:
        user_recent_replies[user_id].append(fallback)

    return (
        {
            "mood": "Witty and Supportive",
            "intent": "chat",
        },
        fallback,
    )


# ============================================================
# KEYBOARD
# ============================================================
def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("💬 Talk", callback_data="home_talk"), types.InlineKeyboardButton("🎮 Games", callback_data="games_menu"))
    markup.add(types.InlineKeyboardButton("🧠 Memory", callback_data="home_memory"), types.InlineKeyboardButton("👤 Profile", callback_data="home_profile"))
    markup.add(types.InlineKeyboardButton("😂 Fun", callback_data="home_fun"), types.InlineKeyboardButton("📊 Stats", callback_data="home_stats"))
    markup.add(types.InlineKeyboardButton("🎙️ Voice", callback_data="home_voice"), types.InlineKeyboardButton("ℹ️ Help", callback_data="home_help"))
    markup.add(types.InlineKeyboardButton("➕ Add To Group", callback_data="home_group"), types.InlineKeyboardButton("🧹 Clear", callback_data="home_clear"))
    return markup


def get_games_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🎯 Guess Number", callback_data="game_guess"), types.InlineKeyboardButton("🎲 Truth or Dare", callback_data="game_tod"))
    markup.add(types.InlineKeyboardButton("🧩 Riddle Battle", callback_data="game_riddle"), types.InlineKeyboardButton("🔥 Roast Battle", callback_data="game_roast"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="home_menu"))
    return markup


def short_name_prefix(user_id, first_name, probability=0.14):
    name = (first_name or "").strip()
    if not name or len(name) > 30:
        return ""
    now = time.time()
    with state_lock:
        last = user_name_mention_time.get(user_id, 0.0)
    if now - last < 600 or random.random() > probability:
        return ""
    with state_lock:
        user_name_mention_time[user_id] = now
    return f"{name}, "

# ============================================================
# GROUP FILTER
# ============================================================

def is_group_message(message):
    return message.chat.type in {"group", "supergroup"}


def should_respond_in_group(message):
    if not RESPOND_IN_GROUPS:
        return False

    if not is_group_message(message):
        return True

    text = message.text or ""
    lowered = text.lower()

    if text.startswith("/"):
        return True

    if BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lowered:
        return True

    reply = message.reply_to_message

    if reply and reply.from_user:
        if BOT_ID and reply.from_user.id == BOT_ID:
            return True

    return False


def strip_bot_mention(text):
    if not text:
        return text

    if BOT_USERNAME:
        text = text.replace(
            f"@{BOT_USERNAME}",
            "",
        )
        text = text.replace(
            f"@{BOT_USERNAME.lower()}",
            "",
        )

    return text.strip()


# ============================================================
# EXTRA FUN / CHAT FEATURES
# ============================================================

JOKES = [
    "Bhai mera future itna bright hai ki phone ki brightness auto low ho gayi 😂",
    "Maine diet start ki thi... phir samose ne aankhon mein aankhein daal di. 😭",
    "Life mein do cheezein fast hain: WiFi kabhi nahi, aur salary ka khatam hona hamesha. 💀",
    "Mera motivation aur Monday ki dosti school ke crush jaisi hai—sirf door se. 😂",
    "Padhai ka sabse bada enemy distraction nahi, '5 minute aur' hai. 😭",
]

SHAYARI = [
    "Dil se nikli baat ko lafzon ka sahara kya,\nTu online ho toh notification se pyara kya. ❤️",
    "Chai garam, mausam suhana,\nDost tu mil jaaye toh scene mastana. ☕❤️",
    "Zindagi chhoti si hai, tension badi bana rakhi hai,\nHans le bhai, duniya ne kaunsi guarantee de rakhi hai. 😌",
    "Tere reply ka intezaar bhi kamaal karta hai,\nEk 'hmm' poora paragraph barbaad karta hai. 😂",
]

FUN_PROMPTS = [
    "🎯 Aaj ka challenge: kisi dost ko bina context 'mission successful' bhej.",
    "🧠 Mini challenge: 10 seconds mein 5 fruits ke naam bol.",
    "😂 Challenge: apne last used emoji se ek sentence bana.",
    "🎭 Challenge: apni life ko ek movie title de.",
    "⚡ Rapid fire: chai ya coffee? Android ya iPhone? Night owl ya early bird?",
]

CHOICE_REPLIES = [
    "Bhai obvious hai — **{choice}** 😎",
    "Mera vote **{choice}** ko. Ab dekhte hain tera choice kitna dangerous hai 😂",
    "**{choice}**. Final answer. Lock kar diya 🔒🔥",
]


def send_help(message):
    bot.reply_to(
        message,
        "ℹ️ **Venu Features**\n\n"
        "💬 Natural Hinglish AI chat + long-term Supabase memory\n"
        "🎮 Guess, Truth/Dare, Riddle, Roast Battle\n"
        "😂 Joke / ❤️ Shayari / 🎲 Fun Zone\n"
        "🎙️ /voice + /novoice for voice replies\n"
        "📊 /stats for activity\n"
        "🧠 /memory for saved profile summary\n"
        "🆔 /id for Telegram IDs\n"
        "🏓 /ping for bot health\n"
        "➕ Add me to a group and mention @Chatbotgebot\n\n"
        "Group mein Venu tab reply karega jab mention ya reply kiya jayega.",
        reply_markup=get_main_keyboard(),
    )


def send_stats(message):
    user_id = message.from_user.id
    today = time.strftime("%Y-%m-%d")
    rows = db.request("GET", f"daily_stats?user_id=eq.{user_id}&order=date.desc&limit=7") or []
    today_row = next((r for r in rows if r.get("date") == today), None)
    total_messages = sum(int(r.get("messages_sent", 0) or 0) for r in rows)
    total_games = sum(int(r.get("games_played", 0) or 0) for r in rows)
    bot.reply_to(
        message,
        "📊 **Venu Stats**\n\n"
        f"📅 Today: {today_row.get('messages_sent', 0) if today_row else 0} messages\n"
        f"🎮 Today games: {today_row.get('games_played', 0) if today_row else 0}\n"
        f"📈 Last 7 days messages: {total_messages}\n"
        f"🏆 Last 7 days games: {total_games}\n"
        f"🎙️ Voice: {'ON' if user_id in TTS_USERS else 'OFF'}",
        reply_markup=get_main_keyboard(),
    )


def send_memory_summary(message):
    memory = get_user_memory(message.from_user.id, message.from_user.first_name or "Dost")
    profile = memory["profile"]
    summary = memory.get("summary") or "No long-term summary yet."
    bot.reply_to(
        message,
        "🧠 **What Venu remembers**\n\n"
        f"Name: {profile.get('name')}\n"
        f"Game: {profile.get('favorite_game')}\n"
        f"Movie: {profile.get('favorite_movie')}\n"
        f"Hobbies: {profile.get('hobbies')}\n"
        f"Relationship: {profile.get('relationship_status')}\n\n"
        f"💭 Context: {summary}",
        reply_markup=get_main_keyboard(),
    )


def send_fun_zone(message):
    bot.reply_to(
        message,
        random.choice(FUN_PROMPTS) + "\n\n" + random.choice(JOKES),
        reply_markup=get_main_keyboard(),
    )


def send_joke(message):
    bot.reply_to(message, "😂 " + random.choice(JOKES), reply_markup=get_main_keyboard())


def send_shayari(message):
    bot.reply_to(message, random.choice(SHAYARI), reply_markup=get_main_keyboard())


def send_add_me_in_group(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "➕ Add Venu To Group",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
    ))
    bot.reply_to(
        message,
        "➕ **Add Venu In Group**\n\n"
        "Button dabao, group select karo aur Venu ko add kar do. 😎🔥\n\n"
        "Group mein mujhe mention karo ya meri message ko reply karo.",
        reply_markup=markup,
    )

# ============================================================
# PROFILE / GROUP / MEMORY
# ============================================================

def send_profile(message):
    try:
        memory = get_user_memory(
            message.from_user.id,
            message.from_user.first_name or "Dost",
        )

        profile = memory["profile"]

        text = (
            "👤 Venu Long-Term Memory Profile\n\n"
            f"📌 Name: {profile.get('name')}\n"
            f"🎂 Age: {profile.get('age')}\n"
            f"🎮 Favorite Game: {profile.get('favorite_game')}\n"
            f"🎬 Favorite Movie: {profile.get('favorite_movie')}\n"
            f"🧠 Mood: {profile.get('current_mood')}\n"
            f"💭 Momentum: {profile.get('emotional_momentum')}"
        )

        bot.reply_to(
            message,
            text,
        )

    except Exception:
        logger.exception("Profile command execution error")


# ============================================================
# TTS
# ============================================================

def send_tts_reply(chat_id, text):
    if not ENABLE_TTS:
        return False

    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3_path = os.path.join(tmp, "venu_reply.mp3")

            gTTS(
                text=text,
                lang="hi",
                slow=False,
            ).save(mp3_path)

            with open(mp3_path, "rb") as audio:
                bot.send_voice(
                    chat_id,
                    audio,
                    caption="🎙️ Venu",
                )

        return True

    except Exception:
        logger.exception("TTS generation failed")
        return False


def transcribe_telegram_voice(message):
    try:
        file_info = bot.get_file(message.voice.file_id)

        downloaded = bot.download_file(file_info.file_path)

        with tempfile.TemporaryDirectory() as tmp:
            ogg_path = os.path.join(tmp, "input.ogg")
            wav_path = os.path.join(tmp, "input.wav")

            with open(ogg_path, "wb") as file:
                file.write(downloaded)

            AudioSegment.from_file(ogg_path).export(
                wav_path,
                format="wav",
            )

            recognizer = sr.Recognizer()

            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)

            return recognizer.recognize_google(
                audio,
                language="hi-IN",
            )

    except sr.UnknownValueError:
        return None

    except Exception:
        logger.exception("Voice transcription failed")
        return None


# ============================================================
# ADMIN
# ============================================================

def admin_only(message):
    return bool(
        ADMIN_ID
        and message.from_user
        and message.from_user.id == ADMIN_ID
    )


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not admin_only(message):
        bot.reply_to(message, "⛔ Admin only.")
        return

    text = message.text.partition(" ")[2].strip()

    if not text:
        bot.reply_to(
            message,
            "Usage: /broadcast your message",
        )
        return

    rows = db.request(
        "GET",
        "users?select=user_id",
    ) or []

    success = 0
    failed = 0

    for row in rows:
        user_id = row.get("user_id")

        if not user_id:
            continue

        try:
            bot.send_message(
                int(user_id),
                text,
            )
            success += 1
            time.sleep(0.04)

        except Exception:
            failed += 1

    bot.reply_to(
        message,
        f"📢 Broadcast finished.\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}",
    )


@bot.message_handler(commands=["refresh"])
def cmd_refresh(message):
    if not admin_only(message):
        bot.reply_to(message, "⛔ Admin only.")
        return

    with state_lock:
        user_memory_cache.clear()
        registered_users_cache.clear()
        user_recent_replies.clear()
        ACTIVE_GAME_SESSIONS.clear()
        last_message_time.clear()

    bot.reply_to(
        message,
        "♻️ Venu caches/state refreshed successfully.",
    )


# ============================================================
# BASIC COMMANDS
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    try:
        user = message.from_user

        register_user(
            user.id,
            user.username,
            user.first_name,
        )

        get_user_memory(
            user.id,
            user.first_name or "Dost",
        )

        bot.reply_to(
            message,
            f"Oye {user.first_name or 'Dost'}! ✨\n"
            "Main Venu hoon. Bata kya scene hai? 😎",
            reply_markup=get_main_keyboard(),
        )

    except Exception:
        logger.exception("Start command execution error")


@bot.message_handler(commands=["profile"])
def cmd_profile(message):
    send_profile(message)


@bot.message_handler(commands=["clear"])
def cmd_clear(message):
    try:
        clear_user_memory(message.from_user.id)

        bot.reply_to(
            message,
            "🧹 Purani chat memory clear kar di. "
            "Naye sire se shuru karte hain 😌✨",
        )

    except Exception:
        logger.exception("Clear command execution error")


@bot.message_handler(commands=["voice"])
def cmd_voice(message):
    user_id = message.from_user.id

    with state_lock:
        TTS_USERS.add(user_id)

    bot.reply_to(
        message,
        "🎙️ Voice replies ON. Ab Venu text ke saath voice bhi de sakta hai.",
    )


@bot.message_handler(commands=["novoice"])
def cmd_novoice(message):
    user_id = message.from_user.id

    with state_lock:
        TTS_USERS.discard(user_id)

    bot.reply_to(
        message,
        "🔇 Voice replies OFF.",
    )


# ============================================================
# EXTRA COMMANDS
# ============================================================

@bot.message_handler(commands=["help"])
def cmd_help(message):
    send_help(message)


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    send_stats(message)


@bot.message_handler(commands=["memory"])
def cmd_memory(message):
    send_memory_summary(message)


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(
        message,
        f"🆔 User ID: `{message.from_user.id}`\n"
        f"💬 Chat ID: `{message.chat.id}`\n"
        f"👥 Chat type: `{message.chat.type}`",
    )


@bot.message_handler(commands=["ping"])
def cmd_ping(message):
    started = time.perf_counter()
    sent = bot.reply_to(message, "🏓 Pinging Venu...")
    ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        bot.edit_message_text(
            f"🏓 **Pong!** {ms} ms\n🤖 Venu is online and ready.",
            message.chat.id,
            sent.message_id,
        )
    except Exception:
        pass


@bot.message_handler(commands=["joke"])
def cmd_joke(message):
    send_joke(message)


@bot.message_handler(commands=["shayari"])
def cmd_shayari(message):
    send_shayari(message)


@bot.message_handler(commands=["fun"])
def cmd_fun(message):
    send_fun_zone(message)


@bot.message_handler(commands=["roast"])
def cmd_roast(message):
    handle_game_manager(message, "roast")


@bot.message_handler(commands=["choose"])
def cmd_choose(message):
    raw = message.text.partition(" ")[2].strip()
    choices = [x.strip() for x in re.split(r"[,|]", raw) if x.strip()]
    if len(choices) < 2:
        bot.reply_to(message, "🎲 Usage: /choose chai, coffee, cold drink")
        return
    bot.reply_to(message, random.choice(CHOICE_REPLIES).format(choice=random.choice(choices)))


@bot.message_handler(commands=["coin"])
def cmd_coin(message):
    bot.reply_to(message, "🪙 " + random.choice(["Heads! 🗣️", "Tails! 🪙"]))


@bot.message_handler(commands=["dice"])
def cmd_dice(message):
    bot.reply_to(message, f"🎲 Dice: **{random.randint(1, 6)}**")



# ============================================================
# GAMES
# ============================================================

RIDDLES = [
    ("Aisi kya cheez hai jo tootne par awaaz nahi karti?", "khamoshi"),
    ("Jitna zyada nikaalo, utna hi bada hota jaata hai. Kya?", "gaddha"),
    ("Mere paas keys hain par locks nahi, space hai par room nahi. Main kya hoon?", "keyboard"),
    ("Subah chaar pair, dopahar do pair, shaam teen pair. Kya?", "insaan"),
    ("Main geela karta hoon jab khud geela hota hoon. Kya?", "towel"),
]

TRUTH_QUESTIONS = [
    "Aisi kaunsi embarrassing cheez hai jo tumne recently ki?",
    "Tumhara sabse weird talent kya hai?",
    "Kis cheez se tum instantly khush ho jaate ho?",
    "Aakhri baar kis baat par jhooth bola tha?",
    "Tumhari sabse funny childhood memory kya hai?",
]

DARES = [
    "Apne last used emoji se ek funny sentence bana.",
    "Kisi friend ko sirf 'mission successful 🫡' bhej.",
    "10 seconds mein 5 fruits ke naam likh.",
    "Apni life ko ek movie title de.",
    "Agle message mein sirf emojis use kar.",
]

ROASTS = [
    "Teri typing dekh ke autocorrect bhi resignation de de. 😂",
    "Tera confidence 4K mein hai, logic 144p mein. 😭",
    "Bhai tu itna unpredictable hai ki random number generator bhi insecure ho jaaye. 😂",
    "Tera plan solid tha... bas plan mein plan hi nahi tha. 💀",
]

def _game_key(user_id):
    return int(user_id)

def handle_game_manager(message, game_type):
    user_id = _game_key(message.from_user.id)
    now = time.time()

    with state_lock:
        ACTIVE_GAME_SESSIONS[user_id] = {
            "type": game_type,
            "created": now,
            "attempts": 0,
        }

    if game_type == "guess":
        secret = random.randint(1, 50)
        with state_lock:
            ACTIVE_GAME_SESSIONS[user_id]["secret"] = secret
            ACTIVE_GAME_SESSIONS[user_id]["attempts"] = 0
        text = "🎮 Guess Number!\n1–50 ke beech number guess kar. Bas number bhej 😎"
    elif game_type == "truth_or_dare":
        text = "🎯 Truth or Dare?\n`truth` ya `dare` bhej."
    elif game_type == "riddle":
        question, answer = random.choice(RIDDLES)
        with state_lock:
            ACTIVE_GAME_SESSIONS[user_id]["question"] = question
            ACTIVE_GAME_SESSIONS[user_id]["answer"] = answer
        text = f"🧩 Riddle Battle!\n\n{question}\n\nAnswer bhej. `/clear` se game reset kar sakte ho."
    elif game_type == "roast":
        text = "🔥 Roast Battle!\n`roast` likh, ya koi line bhej — Venu halka-phulka roast karega. 😈"
    else:
        return

    bot.reply_to(message, text, reply_markup=get_games_keyboard())

def process_active_game(message, user_id, text):
    with state_lock:
        game = ACTIVE_GAME_SESSIONS.get(user_id)

    if not game:
        return False

    game_type = game.get("type")
    raw = text.strip()
    lowered = raw.lower()

    if lowered in {"/cancel", "cancel", "exit", "quit"}:
        with state_lock:
            ACTIVE_GAME_SESSIONS.pop(user_id, None)
        bot.reply_to(message, "🎮 Game cancel. Jab mann ho phir start kar lena 😎")
        return True

    if game_type == "guess":
        try:
            guess = int(raw)
        except ValueError:
            bot.reply_to(message, "🔢 Bhai number bhej, jaise `27`.")
            return True

        if not 1 <= guess <= 50:
            bot.reply_to(message, "1 se 50 ke beech bol 😭")
            return True

        with state_lock:
            game["attempts"] = int(game.get("attempts", 0)) + 1
            secret = int(game["secret"])
            attempts = game["attempts"]

        if guess == secret:
            with state_lock:
                ACTIVE_GAME_SESSIONS.pop(user_id, None)
            bot.reply_to(message, f"🎉 Sahi pakde! Number {secret} tha.\nAttempts: {attempts}")
        elif guess < secret:
            bot.reply_to(message, "📈 Thoda bada number try kar.")
        else:
            bot.reply_to(message, "📉 Thoda chhota number try kar.")
        return True

    if game_type == "truth_or_dare":
        if lowered == "truth":
            answer = random.choice(TRUTH_QUESTIONS)
            with state_lock:
                ACTIVE_GAME_SESSIONS.pop(user_id, None)
            bot.reply_to(message, f"🧠 Truth:\n{answer}")
        elif lowered == "dare":
            answer = random.choice(DARES)
            with state_lock:
                ACTIVE_GAME_SESSIONS.pop(user_id, None)
            bot.reply_to(message, f"🔥 Dare:\n{answer}")
        else:
            bot.reply_to(message, "Sirf `truth` ya `dare` 😎")
        return True

    if game_type == "riddle":
        answer = str(game.get("answer", "")).lower().strip()
        normalized = re.sub(r"[^a-z0-9\u0900-\u097f ]", "", lowered).strip()
        if normalized == answer or answer in normalized or SequenceMatcher(None, normalized, answer).ratio() >= 0.72:
            with state_lock:
                ACTIVE_GAME_SESSIONS.pop(user_id, None)
            bot.reply_to(message, "🎉 Correct! Riddle master nikla tu. 🧠🔥")
        else:
            bot.reply_to(message, "❌ Nope 😭 Ek aur try maar.")
        return True

    if game_type == "roast":
        with state_lock:
            ACTIVE_GAME_SESSIONS.pop(user_id, None)
        bot.reply_to(message, random.choice(ROASTS))
        return True

    with state_lock:
        ACTIVE_GAME_SESSIONS.pop(user_id, None)
    return False


# ============================================================
# INLINE MENU CALLBACKS
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_menu_callback(call):
    try:
        data = call.data or ""
        chat_id = call.message.chat.id if call.message else call.from_user.id
        bot.answer_callback_query(call.id)
        if data == "home_menu":
            bot.edit_message_text("😎 Venu — kya karna hai?", chat_id, call.message.message_id, reply_markup=get_main_keyboard())
        elif data == "games_menu":
            bot.edit_message_text("🎮 Game choose kar:", chat_id, call.message.message_id, reply_markup=get_games_keyboard())
        elif data == "game_guess": handle_game_manager(call.message, "guess")
        elif data == "game_tod": handle_game_manager(call.message, "truth_or_dare")
        elif data == "game_riddle": handle_game_manager(call.message, "riddle")
        elif data == "game_roast": handle_game_manager(call.message, "roast")
        elif data == "home_talk": bot.send_message(chat_id, "Bol bhai 😎")
        elif data == "home_memory": send_memory_summary(call.message)
        elif data == "home_profile": send_profile(call.message)
        elif data == "home_fun": send_fun_zone(call.message)
        elif data == "home_stats": send_stats(call.message)
        elif data == "home_voice": cmd_voice(call.message)
        elif data == "home_help": send_help(call.message)
        elif data == "home_group": send_add_me_in_group(call.message)
        elif data == "home_clear": cmd_clear(call.message)
    except Exception:
        logger.exception("Inline menu callback error")


# ============================================================
# MAIN TEXT HANDLER
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text_message(message):
    try:
        if not should_respond_in_group(message):
            return

        user_id = message.from_user.id
        chat_id = message.chat.id
        with state_lock:
            USER_ACTIVITY[user_id] = time.time()

        text_content = strip_bot_mention(
            message.text or ""
        ).strip()

        if not text_content:
            return

        current_time = time.time()

        with state_lock:
            previous_time = last_message_time.get(user_id)

            if (
                previous_time is not None
                and current_time - previous_time < 1.0
            ):
                return

            last_message_time[user_id] = current_time

        register_user(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
        )

        # Keyboard actions
        if text_content == "🎮 Guess Number":
            handle_game_manager(message, "guess")
            return

        if text_content == "🔥 Roast Battle":
            handle_game_manager(message, "roast")
            return

        if text_content == "🎯 Truth or Dare":
            handle_game_manager(message, "truth_or_dare")
            return

        if text_content == "🧩 Riddle Battle":
            handle_game_manager(message, "riddle")
            return

      

        if text_content == "😂 Joke":
            send_joke(message)
            return

        if text_content == "❤️ Shayari":
            send_shayari(message)
            return

        if text_content == "🎲 Fun Zone":
            send_fun_zone(message)
            return

        if text_content == "📊 My Stats":
            send_stats(message)
            return

        if text_content == "💬 Talk To Venu":
            bot.reply_to(message, "💬 Bol bhai, main sun raha hoon. Aaj ka scene kya hai? 😎", reply_markup=get_main_keyboard())
            return

        if text_content == "🧠 My Memory":
            send_memory_summary(message)
            return

        if text_content == "👤 My Profile":
            send_profile(message)
            return

        if text_content == "🎙️ Voice Mode":
            cmd_voice(message)
            return

        if text_content == "ℹ️ Help":
            send_help(message)
            return

        if text_content == "👤 View Profile":
            send_profile(message)
            return

        if text_content == "➕ Add Me In Group":
            send_add_me_in_group(message)
            return

        if text_content == "🧹 Clear Chat":
            cmd_clear(message)
            return

        # Active game
        if process_active_game(
            message,
            user_id,
            text_content,
        ):
            increment_daily_stats(
                user_id,
                is_game=True,
            )
            return

        # Calculator
        math_result = evaluate_math(text_content)

        if math_result is not None:
            bot.reply_to(
                message,
                f"🧮 Result: {math_result}",
                )

            increment_daily_stats(
                user_id,
                is_game=False,
            )
            return

        # AI
        try:
            bot.send_chat_action(
                chat_id,
                "typing",
            )
        except Exception:
            pass

        save_message(
            user_id,
            "user",
            text_content,
        )

        memory_packet = get_user_memory(
            user_id,
            message.from_user.first_name or "Dost",
        )

        classification, response = generate_unified_ai_response(
            user_id,
            memory_packet,
            text_content,
        )
        prefix = short_name_prefix(user_id, message.from_user.first_name)
        if prefix and not response.lower().startswith((message.from_user.first_name or "").lower() + ","):
            response = prefix + response

        update_profile_field(
            user_id,
            "current_mood",
            classification.get(
                "mood",
                "Witty and Supportive",
            ),
        )

        save_message(
            user_id,
            "assistant",
            response,
        )

        increment_daily_stats(
            user_id,
            is_game=False,
        )

        bot.reply_to(
            message,
            response,
        )

        with state_lock:
            should_tts = user_id in TTS_USERS

        if should_tts:
            threading.Thread(
                target=send_tts_reply,
                args=(chat_id, response),
                daemon=True,
            ).start()

    except Exception:
        logger.exception(
            "Critical error in text handler"
        )

        try:
            bot.reply_to(
                message,
                "Arey yaar, server thoda busy hai 😭 "
                "ek baar phir bol.",
            )
        except Exception:
            pass


# ============================================================
# VOICE HANDLER
# ============================================================

@bot.message_handler(
    content_types=["voice"]
)
def handle_voice_message(message):
    try:
        if not should_respond_in_group(message):
            return

        user_id = message.from_user.id

        register_user(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
        )

        bot.send_chat_action(
            message.chat.id,
            "typing",
        )

        text_content = transcribe_telegram_voice(message)

        if not text_content:
            bot.reply_to(
                message,
                "🎙️ Bhai awaaz clear nahi aayi 😭 "
                "Ek baar thoda clearly bol.",
            )
            return

        # Reuse the same AI pipeline as text.
        save_message(
            user_id,
            "user",
            "[Voice] " + text_content,
        )

        memory_packet = get_user_memory(
            user_id,
            message.from_user.first_name or "Dost",
        )

        classification, response = generate_unified_ai_response(
            user_id,
            memory_packet,
            text_content,
        )

        update_profile_field(
            user_id,
            "current_mood",
            classification.get(
                "mood",
                "Witty and Supportive",
            ),
        )

        save_message(
            user_id,
            "assistant",
            response,
        )

        increment_daily_stats(
            user_id,
            is_game=False,
        )

        bot.reply_to(
            message,
            f"🎙️ Tu bola: {text_content}\n\n"
            f"{response}",
        )

        with state_lock:
            should_tts = ENABLE_TTS and user_id in TTS_USERS

        if should_tts:
            threading.Thread(
                target=send_tts_reply,
                args=(message.chat.id, response),
                daemon=True,
            ).start()

    except Exception:
        logger.exception(
            "Critical error in voice handler"
        )

        try:
            bot.reply_to(
                message,
                "🎙️ Voice processing mein issue aa gaya. "
                "Ek baar phir try kar.",
            )
        except Exception:
            pass


# ============================================================
# BACKGROUND CLEANUP
# ============================================================

def background_cleanup_daemon():
    while True:
        time.sleep(300)

        try:
            current_time = time.time()

            with state_lock:
                stale_games = [
                    user_id
                    for user_id, data
                    in ACTIVE_GAME_SESSIONS.items()
                    if current_time
                    - data.get("created", current_time)
                    > 1800
                ]

                for user_id in stale_games:
                    ACTIVE_GAME_SESSIONS.pop(
                        user_id,
                        None,
                    )

                stale_times = [
                    user_id
                    for user_id, timestamp
                    in last_message_time.items()
                    if current_time - timestamp > 7200
                ]

                for user_id in stale_times:
                    last_message_time.pop(
                        user_id,
                        None,
                    )
                    USER_ACTIVITY.pop(user_id, None)

            logger.info(
                "Background cleanup daemon executed successfully."
            )

        except Exception:
            logger.exception(
                "Background cleanup daemon error"
            )


# ============================================================
# ERROR HANDLER
# ============================================================

# Middleware intentionally disabled for compatibility with pyTelegramBotAPI.
# Incoming updates are already logged inside the message handlers.


# ============================================================
# STARTUP
# ============================================================

def main():
    logger.info(
        "🚀 Starting ULTIMATE Venu AI Telegram Bot — enhanced chat, games, memory, fun and group mode..."
    )
    logger.info("🤖 AI provider: %s | model: %s", AI_BASE_URL, AI_MODEL)

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-keepalive",
    )
    flask_thread.start()

    cleanup_thread = threading.Thread(
        target=background_cleanup_daemon,
        daemon=True,
        name="cleanup-daemon",
    )
    cleanup_thread.start()

    try:
        bot.remove_webhook()
        logger.info(
            "🧹 Existing webhook cleared."
        )
    except Exception:
        logger.exception(
            "Could not remove existing webhook"
        )

    while True:
        try:
            logger.info(
                "🔄 Telegram polling started..."
            )

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=[
                    "message",
                    "edited_message",
                    "callback_query",
                ],
            )

        except KeyboardInterrupt:
            logger.info(
                "🛑 Bot stopped by user."
            )
            break

        except Exception:
            logger.exception(
                "Polling exception. Reconnecting in 5 seconds..."
            )
            time.sleep(5)


if __name__ == "__main__":
    main()

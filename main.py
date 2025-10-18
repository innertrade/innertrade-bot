# main.py — Innertrade Kai Mentor Bot
# Версия: 2025-09-26 (v8.0-safe: coach-engine + безопасный импорт, гарантированный app)

from __future__ import annotations
import os, json, time, logging, threading, hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, List

from flask import Flask, request, abort, jsonify
import telebot
from telebot import types
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
from openai import OpenAI

# --- создаём app СРАЗУ, чтобы он точно существовал для gunicorn: ---
app = Flask(__name__)

# --- попытка импортировать слой логики; при сбое — заглушка ---
try:
    import logic_layer as LG  # файл logic_layer.py должен лежать рядом
except Exception as _e:
    logging.getLogger("kai-mentor").error(f"logic_layer import failed: {_e}")

    class _LGStub:
        @staticmethod
        def extract_problem_summary(history: List[Dict]) -> str:
            return "Триггеры: нужен пример"

        @staticmethod
        def process_turn(oai_client, model: str, state: Dict[str, Any], user_text: str) -> Dict[str, Any]:
            # простой ручной режим (2-3 уточняющих шага)
            coach = state.get("coach", {})
            turns = int(coach.get("turns", 0)) + 1
            reply = "Давай на конкретном примере: где и когда это было, что именно сделал не по плану?"
            if turns >= 2:
                reply = "Окей, всё ближе к сути. Сформулируй в одной фразе, что мешает прямо сейчас?"
            return {
                "reply": reply, "state_updates": {"coach": {"turns": turns, "loop": "explore"}},
                "ask_confirm": False, "propose_summary": "", "suggest_struct": False
            }
    LG = _LGStub()

# ========= Version =========
def _code_hash():
    try:
        with open(__file__, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "unknown"
BOT_VERSION = f"2025-09-26-{_code_hash()}"

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL     = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "webhook").strip()
TG_SECRET      = os.getenv("TG_WEBHOOK_SECRET", "").strip()
DATABASE_URL   = os.getenv("DATABASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

OFFSCRIPT_ENABLED = os.getenv("OFFSCRIPT_ENABLED", "true").lower() == "true"
SET_WEBHOOK_FLAG  = os.getenv("SET_WEBHOOK", "false").lower() == "true"
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BODY          = int(os.getenv("MAX_BODY", "1000000"))
IDLE_MINUTES_REMIND = int(os.getenv("IDLE_MINUTES_REMIND", "60"))
IDLE_MINUTES_RESET  = int(os.getenv("IDLE_MINUTES_RESET", "240"))
REMINDERS_ENABLED   = os.getenv("REMINDERS_ENABLED", "true").lower() == "true"
HIST_LIMIT = 16

# ========= Logging =========
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("kai-mentor")
log.info(f"Starting bot version: {BOT_VERSION}")

# ========= Guards (без KeyError) =========
_required = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "PUBLIC_URL": PUBLIC_URL,
    "WEBHOOK_PATH": WEBHOOK_PATH,
    "TG_WEBHOOK_SECRET": TG_SECRET,
    "DATABASE_URL": DATABASE_URL,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    # Не падаем раньше времени: вернём 500 на /version, но app уже создан.
    log.error("ENV variables missing: %s", ", ".join(_missing))

# ========= Intents/Steps =========
INTENT_GREET = "greet"
INTENT_FREE  = "free"
INTENT_ERR   = "error"
INTENT_DONE  = "done"

STEP_ASK_STYLE  = "ask_style"
STEP_FREE_INTRO = "free_intro"
STEP_ERR_DESCR  = "err_describe"
STEP_MER_CTX    = "mer_context"
STEP_MER_EMO    = "mer_emotions"
STEP_MER_THO    = "mer_thoughts"
STEP_MER_BEH    = "mer_behavior"
STEP_GOAL       = "goal_positive"
STEP_TOTE_OPS   = "tote_ops"
STEP_TOTE_TEST  = "tote_test"
STEP_TOTE_EXIT  = "tote_exit"
MER_ORDER = [STEP_MER_CTX, STEP_MER_EMO, STEP_MER_THO, STEP_MER_BEH]

# ========= OpenAI =========
oai_client = None
openai_status = "disabled"
if OPENAI_API_KEY and OFFSCRIPT_ENABLED:
    try:
        oai_client = OpenAI(api_key=OPENAI_API_KEY)
        oai_client.chat.completions.create(
            model=OPENAI_MODEL, messages=[{"role": "user", "content": "ping"}], max_tokens=4
        )
        openai_status = "active"
        log.info("OpenAI ready")
    except Exception as e:
        log.error(f"OpenAI init error: {e}")
        oai_client = None
        openai_status = f"error: {e}"

# ========= DB =========
engine = create_engine(
    DATABASE_URL or "sqlite:///tmp.db",  # чтобы app поднялся даже если нет БД (для диагностики /version)
    poolclass=QueuePool, pool_size=5, max_overflow=10, pool_timeout=30, pool_recycle=1800,
)
def db_exec(sql: str, params: Optional[Dict[str, Any]] = None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def init_db():
    if not DATABASE_URL:
        log.warning("DATABASE_URL is empty — DB init skipped (diagnostic mode)")
        return
    db_exec("""
    CREATE TABLE IF NOT EXISTS user_state(
        user_id BIGINT PRIMARY KEY,
        intent TEXT,
        step TEXT,
        data TEXT,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_updated_at ON user_state(updated_at)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_user_state_intent_step ON user_state(intent, step)")
    log.info("DB initialized")

# ========= State =========
def load_state(uid: int) -> Dict[str, Any]:
    try:
        row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": uid}).mappings().first()
    except Exception as e:
        log.error("load_state DB error: %s", e)
        row = None
    if row:
        data = {}
        if row["data"]:
            try:
                data = json.loads(row["data"])
            except Exception as e:
                log.error("Failed to parse user data: %s", e)
                data = {}
        if "history" not in data:
            data["history"] = []
        return {"user_id": uid, "intent": row["intent"] or INTENT_GREET, "step": row["step"] or STEP_ASK_STYLE, "data": data}
    return {"user_id": uid, "intent": INTENT_GREET, "step": STEP_ASK_STYLE, "data": {"history": []}}

def save_state(uid: int, intent=None, step=None, data=None) -> Dict[str, Any]:
    cur = load_state(uid)
    intent = intent or cur["intent"]
    step   = step   or cur["step"]
    new_data = cur["data"].copy()
    if data:
        new_data.update(data)
    new_data["last_state_write_at"] = datetime.now(timezone.utc).isoformat()
    try:
        db_exec("""
            INSERT INTO user_state (user_id, intent, step, data, updated_at)
            VALUES (:uid, :intent, :step, :data, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
            SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=CURRENT_TIMESTAMP
        """, {"uid": uid, "intent": intent, "step": step, "data": json.dumps(new_data, ensure_ascii=False)})
    except Exception as e:
        log.error("save_state DB error: %s", e)
    return {"user_id": uid, "intent": intent, "step": step, "data": new_data}

# ========= Bot =========
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)

MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно", "🤔 Не знаю, с чего начать")

STYLE_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
STYLE_KB.row("ты", "вы")

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _append_history(data: Dict[str, Any], role: str, content: str) -> Dict[str, Any]:
    hist = data.get("history", [])
    if len(hist) >= HIST_LIMIT:
        hist = hist[-(HIST_LIMIT - 1):]
    hist.append({"role": role, "content": content})
    data["history"] = hist
    return data

def mer_prompt_for(step: str) -> str:
    return {
        "mer_context": "Зафиксируем картинку. Где и когда это было? Коротко.",
        "mer_emotions": "Что почувствовал в моменте (2–3 слова)?",
        "mer_thoughts": "Какие мысли мелькали (2–3 коротких фразы)?",
        "mer_behavior": "Что сделал фактически? Действия.",
    }.get(step, "Продолжим.")

def offer_structural(uid: int, st: Dict[str, Any]):
    if st["data"].get("struct_offer_shown"):
        return
    st["data"]["struct_offer_shown"] = True
    save_state(uid, data=st["data"])
    summary = LG.extract_problem_summary(st["data"].get("history", []))
    kb = types.InlineKeyboardMarkup().row(
        types.InlineKeyboardButton("Разобрать по шагам", callback_data="start_error_flow"),
        types.InlineKeyboardButton("Пока нет", callback_data="skip_error_flow")
    )
    bot.send_message(uid, f"{summary}\n\nГотов разобрать это по шагам?", reply_markup=kb)

# ========= Handlers =========
@bot.message_handler(commands=["start", "reset"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    st = load_state(uid)
    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        pass
    else:
        st = save_state(uid, INTENT_GREET, STEP_ASK_STYLE, {"history": []})
    bot.send_message(uid,
        "👋 Привет! Как удобнее — <b>ты</b> или <b>вы</b>?\n\n"
        "Если захочешь начать с чистого листа — напиши: <b>новый разбор</b>.",
        reply_markup=STYLE_KB
    )

@bot.message_handler(commands=["version", "v"])
def cmd_version(m: types.Message):
    info = (
        f"🔄 Версия бота: {BOT_VERSION}\n"
        f"📝 Хэш кода: {_code_hash()}\n"
        f"🕒 Время сервера: {datetime.now(timezone.utc).isoformat()}\n"
        f"🤖 OpenAI: {openai_status}"
    )
    bot.reply_to(m, info)

@bot.message_handler(commands=["menu"])
def cmd_menu(m: types.Message):
    bot.send_message(m.chat.id, "Меню:", reply_markup=MAIN_MENU)

@bot.message_handler(content_types=["text"])
def on_text(m: types.Message):
    handle_text_message(m.from_user.id, m.text.strip(), m)

# ========= Основной обработчик =========
def handle_text_message(uid: int, text_in: str, original_message=None):
    st = load_state(uid)
    log.info("User %s: intent=%s step=%s text='%s'", uid, st["intent"], st["step"], text_in[:200])

    if text_in.lower().strip() in ("новый разбор", "новый", "с чистого листа", "start over"):
        st = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, {"history": [], "struct_offer_shown": False})
        bot.send_message(uid, "Окей, начнём с чистого листа. Что сейчас в трейдинге хочется поправить?", reply_markup=MAIN_MENU)
        return

    st["data"] = _append_history(st["data"], "user", text_in)
    st["data"]["last_user_msg_at"] = _now_iso()
    st["data"]["awaiting_reply"] = True

    if st["intent"] == INTENT_GREET and st["step"] == STEP_ASK_STYLE:
        if text_in.lower() in ("ты", "вы"):
            st["data"]["style"] = text_in.lower()
            st = save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
            bot.send_message(uid, f"Принято ({text_in}). С чего начнём? Что сейчас в трейдинге хочется поправить?", reply_markup=MAIN_MENU)
        else:
            save_state(uid, data=st["data"])
            bot.send_message(uid, "Выбери «ты» или «вы».", reply_markup=STYLE_KB)
        return

    if st["intent"] == INTENT_ERR:
        proceed_struct(uid, text_in, st)
        return

    # КОУЧ-ДВИЖОК
    if oai_client and OFFSCRIPT_ENABLED:
        decision = LG.process_turn(oai_client=oai_client, model=OPENAI_MODEL, state=st["data"], user_text=text_in)
        if decision.get("state_updates"):
            st["data"].setdefault("coach", {})
            for k, v in decision["state_updates"].items():
                if isinstance(v, dict) and isinstance(st["data"].get(k), dict):
                    st["data"][k].update(v)
                else:
                    st["data"][k] = v
        reply = decision.get("reply") or "Окей. Где именно отступил от плана — вход/стоп/выход?"
        st["data"] = _append_history(st["data"], "assistant", reply)
        if decision.get("propose_summary"):
            st["data"]["problem_draft"] = decision["propose_summary"]
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        if original_message:
            bot.reply_to(original_message, reply, reply_markup=MAIN_MENU)
        else:
            bot.send_message(uid, reply, reply_markup=MAIN_MENU)
        if decision.get("ask_confirm") and st["data"].get("problem_draft"):
            kb = types.InlineKeyboardMarkup().row(
                types.InlineKeyboardButton("Да, это оно", callback_data="confirm_problem"),
                types.InlineKeyboardButton("Нет, переформулировать", callback_data="refine_problem"),
            )
            bot.send_message(uid, f"Суммирую твоими словами:\n\n<b>{st['data']['problem_draft']}</b>\n\nПодходит?", reply_markup=kb)
            return
        if decision.get("suggest_struct") or st["data"].get("problem_confirmed"):
            offer_structural(uid, st)
        return
    else:
        turns = int(st["data"].get("coach", {}).get("turns", 0)) + 1
        st["data"].setdefault("coach", {})["turns"] = turns
        reply = "Давай уточним: где и когда это было, что именно сделал не по плану?" if turns <= 2 else "Почти там. С одной фразой — в чём затык?"
        st["data"] = _append_history(st["data"], "assistant", reply)
        save_state(uid, INTENT_FREE, STEP_FREE_INTRO, st["data"])
        bot.send_message(uid, reply, reply_markup=MAIN_MENU)
        return

# ========= Structural Flow =========
def proceed_struct(uid: int, text_in: str, st: Dict[str, Any]):
    step = st["step"]
    data = st["data"].copy()

    if step == STEP_ERR_DESCR:
        data["error_description"] = text_in
        save_state(uid, INTENT_ERR, STEP_MER_CTX, data)
        bot.send_message(uid, mer_prompt_for(STEP_MER_CTX), reply_markup=MAIN_MENU)
        return

    if step in MER_ORDER:
        mer = data.get("mer", {})
        mer[step] = text_in
        data["mer"] = mer
        idx = MER_ORDER.index(step)
        if idx + 1 < len(MER_ORDER):
            nxt = MER_ORDER[idx + 1]
            save_state(uid, INTENT_ERR, nxt, data)
            bot.send_message(uid, mer_prompt_for(nxt), reply_markup=MAIN_MENU)
        else:
            save_state(uid, INTENT_ERR, STEP_GOAL, data)
            bot.send_message(uid, "Сформулируй позитивную цель: что будешь делать вместо прежнего поведения?", reply_markup=MAIN_MENU)
        return

    if step == STEP_GOAL:
        data["goal"] = text_in
        save_state(uid, INTENT_ERR, STEP_TOTE_OPS, data)
        bot.send_message(uid, "Для ближайших 3 сделок назови 2–3 конкретных шага (коротко, списком).", reply_markup=MAIN_MENU)
        return

    if step == STEP_TOTE_OPS:
        tote = data.get("tote", {})
        tote["ops"] = text_in
        data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_TEST, data)
        bot.send_message(uid, "Как поймёшь, что получилось? Один простой критерий.", reply_markup=MAIN_MENU)
        return

    if step == STEP_TOTE_TEST:
        tote = data.get("tote", {})
        tote["test"] = text_in
        data["tote"] = tote
        save_state(uid, INTENT_ERR, STEP_TOTE_EXIT, data)
        bot.send_message(uid, "Если проверка покажет «не получилось» — что сделаешь?", reply_markup=MAIN_MENU)
        return

    if step == STEP_TOTE_EXIT:
        tote = data.get("tote", {})
        tote["exit"] = text_in
        data["tote"] = tote
        mer = data.get('mer', {})
        summary = [
            "<b>Итог разбора</b>",
            f"Проблема: {data.get('error_description', data.get('problem', '—'))}",
            f"Контекст: {mer.get(STEP_MER_CTX, '—')}",
            f"Эмоции: {mer.get(STEP_MER_EMO, '—')}",
            f"Мысли: {mer.get(STEP_MER_THO, '—')}",
            f"Поведение: {mer.get(STEP_MER_BEH, '—')}",
            f"Цель: {data.get('goal', '—')}",
            f"Шаги (3 сделки): {data.get('tote', {}).get('ops', '—')}",
            f"Проверка: {data.get('tote', {}).get('test', '—')}",
            f"Если не вышло: {data.get('tote', {}).get('exit', '—')}",
        ]
        save_state(uid, INTENT_DONE, STEP_FREE_INTRO, data)
        bot.send_message(uid, "\n".join(summary), reply_markup=MAIN_MENU)
        bot.send_message(uid, "Готов вынести это в «фокус недели» или идём дальше?", reply_markup=MAIN_MENU)
        return

    save_state(uid, INTENT_FREE, STEP_FREE_INTRO, data)
    bot.send_message(uid, "Окей, вернёмся на шаг назад и уточним ещё чуть-чуть.", reply_markup=MAIN_MENU)

# ========= HTTP =========
@app.get("/")
def root():
    ok_env = not _missing
    return jsonify({"ok": ok_env, "time": _now_iso(), "missing_env": _missing})

@app.get("/version")
def version_api():
    return jsonify({"version": BOT_VERSION, "code_hash": _code_hash(), "status": "running", "timestamp": _now_iso(), "openai": openai_status})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413, description="Body too large")
    body = request.get_data()
    if not body:
        abort(400, description="Empty body")
    try:
        update = telebot.types.Update.de_json(body.decode("utf-8"))
        if update is None:
            abort(400, description="Invalid update")
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        log.error("Webhook processing error: %s", e)
        abort(500)

# ========= Init on import (для gunicorn) =========
try:
    init_db()
    log.info("DB initialized (import)")
except Exception as e:
    log.error("DB init (import) failed: %s", e)

if os.getenv("SET_WEBHOOK","false").lower() == "true":
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=f"{PUBLIC_URL}/{WEBHOOK_PATH}",
            secret_token=TG_SECRET,
            allowed_updates=["message", "callback_query"]
        )
        log.info("Webhook set to %s/%s", PUBLIC_URL, WEBHOOK_PATH)
    except Exception as e:
        log.error("Webhook setup error: %s", e)

try:
    th = threading.Thread(target=lambda: None, daemon=True)
    th.start()
except Exception as e:
    log.error("Can't start background thread: %s", e)

# ========= Dev run =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

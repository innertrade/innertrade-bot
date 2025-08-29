import os, json, time
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from sqlalchemy import create_engine, text

# --- ENV ---
TG_TOKEN         = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL     = os.getenv("DATABASE_URL")
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "webhook")
PUBLIC_URL       = os.getenv("PUBLIC_URL")  # https://<your-app>.onrender.com
BOT_NAME         = os.getenv("BOT_NAME", "Kai Mentor Bot")
APP_VERSION      = os.getenv("APP_VERSION", "greet-stable-2025-08-29")

if not TG_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN missing")
if not DATABASE_URL:raise RuntimeError("DATABASE_URL missing")
if not PUBLIC_URL:  raise RuntimeError("PUBLIC_URL missing")

# --- App / DB / Bot ---
app = Flask(__name__)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML", threaded=False)

# --- DB helpers ---
def load_state(user_id: int):
    sql = text("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, 'greet', 'ask_form', '{}'::jsonb, now())
        ON CONFLICT (user_id) DO NOTHING;
        SELECT intent, step, COALESCE(data, '{}'::jsonb) AS data
        FROM user_state WHERE user_id=:uid;
    """)
    with engine.begin() as conn:
        res = conn.execute(sql, {"uid": user_id}).fetchone()
        if not res:
            return {"intent":"greet","step":"ask_form","data":{}}
        return {"intent":res.intent, "step":res.step, "data":dict(res.data)}

def save_state(user_id: int, intent=None, step=None, patch_data: dict | None=None):
    # merge JSONB patch (shallow)
    set_bits = []
    params = {"uid": user_id}
    if intent is not None:
        set_bits.append("intent=:intent")
        params["intent"] = intent
    if step is not None:
        set_bits.append("step=:step")
        params["step"] = step
    if patch_data:
        set_bits.append("data = COALESCE(data, '{}'::jsonb) || :data::jsonb")
        params["data"] = json.dumps(patch_data, ensure_ascii=False)
    if not set_bits:
        set_bits.append("updated_at=now()")
    sql = text(f"""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, COALESCE(:intent,'greet'), COALESCE(:step,'ask_form'),
                COALESCE(:data,'{{}}')::jsonb, now())
        ON CONFLICT (user_id) DO UPDATE
        SET {", ".join(set_bits)}, updated_at=now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, params)

# --- UI helpers ---
def kb_yes_no(yes="Да", no="Нет"):
    m = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    m.add(KeyboardButton(yes), KeyboardButton(no))
    return m

def kb_tu_vy():
    m = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    m.add(KeyboardButton("ты"), KeyboardButton("вы"))
    return m

def greet_text(first_name: str, address: str|None):
    base = f"👋 Привет{',' if first_name else ''} {first_name}!" if first_name else "👋 Привет!"
    if not address:
        return base + "\nКак удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)"
    else:
        # нейтральное приветствие после выбора формы
        return base + "\nМожем просто поговорить — напиши, что сейчас болит в торговле. Или выбери пункт ниже."

def t(addr: str|None, tu: str, vy: str) -> str:
    # простое согласование формы
    return tu if addr == "ty" else vy

# --- Core behaviour ---
def ensure_address(user_id: int, chat_id: int, first_name: str|None):
    st = load_state(user_id)
    addr = st["data"].get("address")
    if addr in ("ty","vy"):
        return st
    # спросить форму
    save_state(user_id, intent="greet", step="ask_form", patch_data={})
    bot.send_message(chat_id, greet_text(first_name or "", None), reply_markup=kb_tu_vy())
    return None  # мы отправили вопрос и выходим

def handle_address_choice(user_id: int, chat_id: int, msg_text: str, first_name: str|None):
    val = msg_text.strip().lower()
    if val not in ("ты","вы"):
        bot.send_message(chat_id, "Напиши, пожалуйста, одно слово: <b>ты</b> или <b>вы</b>.", reply_markup=kb_tu_vy())
        return
    addr = "ty" if val == "ты" else "vy"
    save_state(user_id, intent="greet", step="free_talk", patch_data={"address": addr})
    bot.send_message(chat_id, greet_text(first_name or "", addr))
    # мягкий старт
    bot.send_message(chat_id, t(addr,
        "Если хочешь, просто расскажи, что болит в торговле — я слушаю.",
        "Если удобно, просто расскажите, что сейчас болит в торговле — я слушаю."
    ))

# --- Flask routes ---
@app.get("/")
def root():
    return "ok", 200

@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

@app.get("/status")
def status():
    # без user-context просто отдать версию
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "version": APP_VERSION})

# --- Telegram webhook ---
@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("Content-Type") != "application/json":
        return "bad", 400
    update = request.get_json(silent=True)
    if not update:
        return "bad", 400
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "ok", 200

# --- Commands ---
@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = load_state(m.from_user.id)
    bot.reply_to(m, json.dumps({
        "ok": True,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "intent": st["intent"], "step": st["step"], "db": "ok"
    }, ensure_ascii=False, indent=2))

@bot.message_handler(commands=["reset", "start"])
def cmd_reset(m):
    # сброс до приветствия с выбором формы
    save_state(m.from_user.id, intent="greet", step="ask_form", patch_data={"address": None})
    bot.send_message(m.chat.id, greet_text(m.from_user.first_name or "", None), reply_markup=kb_tu_vy())

# --- Text handler ---
@bot.message_handler(func=lambda msg: True, content_types=["text"])
def on_text(m):
    user_id = m.from_user.id
    chat_id = m.chat.id
    txt = (m.text or "").strip()

    # 1) Проверяем/запрашиваем форму обращения
    st = ensure_address(user_id, chat_id, m.from_user.first_name)
    if st is None:
        return  # уже спросили "ты/вы"

    addr = st["data"].get("address")  # "ty" | "vy"
    intent = st["intent"]
    step = st["step"]

    # 2) Если мы на шаге выбора формы — обрабатываем
    if step == "ask_form":
        return handle_address_choice(user_id, chat_id, txt, m.from_user.first_name)

    # 3) «Софт-старт»: первые реплики без жёсткой схемы
    if intent in ("greet","chat") and step in ("free_talk", None):
        # простые ключи: помогать говорить, задавать мягкий уточняющий вопрос
        if len(txt) < 3:
            bot.send_message(chat_id, t(addr,
                "Расскажи чуть подробнее — что именно мешает сейчас в торговле?",
                "Расскажите чуть подробнее — что именно мешает сейчас в торговле?"
            ))
            return
        # Мягкое переформулирование без «цитирования дословно»
        bot.send_message(chat_id, t(addr,
            "Слышу: есть трудности с дисциплиной в моменте сделки. Хочешь, аккуратно разберём это по шагам?",
            "Слышу: есть трудности с дисциплиной в моменте сделки. Хотите, аккуратно разберём это по шагам?"
        ), reply_markup=kb_yes_no(yes=t(addr, "Да, разберём", "Да, разберём"), no=t(addr,"Пока поговорим","Пока поговорим")))
        save_state(user_id, intent="chat", step="offer_deep_dive")
        return

    if intent == "chat" and step == "offer_deep_dive":
        if txt.lower() in ("да","да, разберём","да разберём","разберём","ок","хочу"):
            # Здесь дальше подключите ваш сценарий MERCEDES/TOTE или GPT-offscript — но форма обращения уже стабильна.
            bot.send_message(chat_id, t(addr,
                "Окей. Начнём с краткой фиксации ошибки в 1–2 предложениях (на уровне действия). После — пойдём шаг за шагом.",
                "Окей. Начнём с краткой фиксации ошибки в 1–2 предложениях (на уровне действия). После — пойдём шаг за шагом."
            ))
            save_state(user_id, intent="error_flow", step="ask_error")
        else:
            bot.send_message(chat_id, t(addr,
                "Хорошо. Тогда просто расскажи, что ещё тревожит — я тут.",
                "Хорошо. Тогда просто расскажите, что ещё тревожит — я тут."
            ))
        return

    # Заглушка на случай если пользователь уже в сценарии, но написал что-то иное
    if intent == "error_flow":
        if step == "ask_error":
            # принятие первой формулировки и переход дальше
            save_state(user_id, intent="error_flow", step="mercedes_start",
                       patch_data={"last_error": txt})
            bot.send_message(chat_id, t(addr,
                "Принято. Перейдём к разбору. Скажи, пожалуйста, в какой ситуации это обычно происходит (1–2 предложения)?",
                "Принято. Перейдём к разбору. Скажите, пожалуйста, в какой ситуации это обычно происходит (1–2 предложения)?"
            ))
            return
        # Остальные шаги сценария реализуются в вашем «сценарном» модуле.
        bot.send_message(chat_id, t(addr,
            "Я запомнил последнюю формулировку. Можем продолжать разбор или вернуться к свободному разговору.",
            "Я запомнил последнюю формулировку. Можем продолжать разбор или вернуться к свободному разговору."
        ))
        return

    # Фолбэк
    bot.send_message(chat_id, t(addr,
        "Понял. Можем поговорить свободно или перейти к разбору. Что предпочтёшь?",
        "Понял. Можем поговорить свободно или перейти к разбору. Что предпочтёте?"
    ))

# --- Local run (Render запускает как `python main.py`) ---
if __name__ == "__main__":
    # Локально без вебхука можно включить polling при необходимости, но для Render используем вебхук
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

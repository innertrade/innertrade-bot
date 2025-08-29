import os, json, time, logging
from datetime import datetime, timezone
from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ───────────────────────────
# Env / Config
# ───────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
DATABASE_URL     = os.getenv("DATABASE_URL", "")
PUBLIC_URL       = os.getenv("PUBLIC_URL", "")
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "webhook")
TG_WEBHOOK_SECRET= os.getenv("TG_WEBHOOK_SECRET", "")
ALLOW_SET_WEBHOOK= os.getenv("ALLOW_SET_WEBHOOK", "0") == "1"

if not TELEGRAM_TOKEN:  raise RuntimeError("TELEGRAM_TOKEN missing")
if not DATABASE_URL:    raise RuntimeError("DATABASE_URL missing")
if not PUBLIC_URL:      raise RuntimeError("PUBLIC_URL missing (https://<your-app>.onrender.com)")
if not WEBHOOK_PATH:    raise RuntimeError("WEBHOOK_PATH missing")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

MAX_BODY_BYTES   = 1_000_000
BOT_NAME         = "Kai Mentor Bot"

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("innertrade")

# ───────────────────────────
# App / DB / Bot
# ───────────────────────────
app = Flask(__name__)
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# ───────────────────────────
# DB bootstrap (idempotent)
# ───────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS users(
  user_id BIGINT PRIMARY KEY,
  mode TEXT NOT NULL DEFAULT 'course',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_state(
  user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  intent  TEXT,
  step    TEXT,
  data    JSONB,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Урок 1 (минимум, без лишних полей — нарастим позже)
CREATE TABLE IF NOT EXISTS errors(
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  error_text TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_errors_user ON errors(user_id);
"""

def db_exec(sql, params=None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_schema():
    db_exec(DDL)

def ensure_user(uid:int):
    db_exec("INSERT INTO users(user_id) VALUES (:u) ON CONFLICT DO NOTHING", {"u":uid})
    db_exec("""
        INSERT INTO user_state(user_id,intent,step,data)
        VALUES(:u,'greet','ask_form','{}'::jsonb)
        ON CONFLICT (user_id) DO NOTHING
    """, {"u":uid})

def get_state(uid:int):
    row = db_exec("SELECT intent,step,data FROM user_state WHERE user_id=:u", {"u":uid}).mappings().first()
    if not row:
        ensure_user(uid)
        row = {"intent":"greet","step":"ask_form","data":{}}
    else:
        row = dict(row)
        row["data"] = row["data"] or {}
    return row

def set_state(uid:int, intent:str=None, step:str=None, data:dict=None):
    cur = get_state(uid)
    if intent is None: intent = cur["intent"]
    if step   is None: step   = cur["step"]
    if data   is None: data   = cur["data"]
    db_exec("""
      INSERT INTO user_state(user_id,intent,step,data,updated_at)
      VALUES(:u,:i,:s,:d, now())
      ON CONFLICT (user_id) DO UPDATE
      SET intent=:i, step=:s, data=:d, updated_at=now()
    """, {"u":uid,"i":intent,"s":step,"d":json.dumps(data)})

def save_error(uid:int, text_err:str):
    db_exec("INSERT INTO errors(user_id,error_text) VALUES (:u,:t)", {"u":uid,"t":text_err})

# ───────────────────────────
# Helpers
# ───────────────────────────
def reply_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка","🧩 Хочу стратегию")
    kb.row("📄 Паспорт","🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл","🤔 Не знаю с чего начать")
    return kb

def greet_text(first_name:str, you_form:str|None):
    base = f"👋 Привет, {first_name or 'трейдер'}!\nМожем просто поговорить — напиши, что болит в торговле. Или выбери пункт ниже."
    if not you_form:
        base += "\n\nКак удобнее обращаться — *ты* или *вы*? (можешь написать одно слово)"
    return base

def is_smalltalk(txt:str)->bool:
    t = txt.lower().strip()
    return t in {"привет","здарова","привет!","hi","hello","йо","ку"} or t.startswith("как дела")

def normalize_you_form(txt:str)->str|None:
    t = txt.strip().lower()
    if t in {"ты","на ты"}: return "ты"
    if t in {"вы","на вы"}: return "вы"
    return None

def ask_mercedes_block(block:str)->str:
    MAP = {
      "context":"КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
      "emotions":"ЭМОЦИИ. Что чувствуешь в момент ошибки? (несколько слов)",
      "thoughts":"МЫСЛИ. Что говоришь себе в этот момент? (1–2 фразы)",
      "behavior":"ПОВЕДЕНИЕ. Что именно ты делаешь? (1–2 предложения, глаголами)"
    }
    return MAP.get(block,"")

def mercedes_order():
    return ["context","emotions","thoughts","behavior"]

def done_has_error_sentence(txt:str)->bool:
    # минимальная валидация: 1–2 предложения «по делу»
    return len(txt.split()) >= 3

# ───────────────────────────
# Telegram commands
# ───────────────────────────
@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=['status'])
def cmd_status(m):
    try:
        st = get_state(m.from_user.id)
        return bot.reply_to(m, "```\n"+json.dumps({
            "ok": True,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "intent": st["intent"],
            "step": st["step"],
            "db":"ok"
        }, ensure_ascii=False, indent=2)+"\n```", parse_mode="Markdown")
    except SQLAlchemyError:
        return bot.reply_to(m, "```\n"+json.dumps({
            "ok": False,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "db":"error"
        }, ensure_ascii=False, indent=2)+"\n```", parse_mode="Markdown")

@bot.message_handler(commands=['reset','start'])
def cmd_reset(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="greet", step="ask_form", data={"you_form":None,"name":m.from_user.first_name})
    bot.send_message(uid, greet_text(m.from_user.first_name, None), reply_markup=reply_kb())

# ───────────────────────────
# Intent buttons
# ───────────────────────────
@bot.message_handler(func=lambda m: m.text in {"🚑 У меня ошибка"})
def btn_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="error", step="ask_error", data={})
    bot.send_message(uid,
        "Опиши основную ошибку 1–2 предложениями. Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю на первой коррекции».",
        reply_markup=reply_kb()
    )

# ───────────────────────────
# Free talk + FSM
# ───────────────────────────
@bot.message_handler(func=lambda m: True, content_types=['text'])
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()
    ensure_user(uid)
    st = get_state(uid)
    data = st["data"] or {}
    you_form = data.get("you_form")
    name = data.get("name") or m.from_user.first_name

    # 1) Greet phase: capture "ты/вы" once, then free talk
    if st["intent"] == "greet":
        if st["step"] == "ask_form":
            choice = normalize_you_form(txt)
            if choice:
                data["you_form"] = choice
                set_state(uid, step="free_talk", data=data)
                return bot.send_message(uid,
                    f"Принято ({choice}). Можем просто поговорить — расскажи, что сейчас болит, или выбери пункт ниже.",
                    reply_markup=reply_kb())
            # если юзер не ответил «ты/вы», но пишет «привет/боль»
            if is_smalltalk(txt) or len(txt) >= 1:
                # не давим, продолжаем мягко
                return bot.send_message(uid, greet_text(name, you_form), reply_markup=reply_kb())

        # после ask_form
        if st["step"] == "free_talk":
            # если человек чётко описал проблему — мягко предлагаем разбор
            if len(txt.split()) >= 5:
                set_state(uid, intent="error", step="ask_error", data={})
                return bot.send_message(uid,
                    "Понял. Зафиксирую это как отправную точку.\n\nНапиши коротко основную ошибку (1–2 предложения), и мы пройдёмся по шагам.",
                    reply_markup=reply_kb())
            else:
                return bot.send_message(uid, "Расскажи немного подробнее — что именно болит в сделках?", reply_markup=reply_kb())

    # 2) Error flow
    if st["intent"] == "error":
        # (a) ask_error
        if st["step"] == "ask_error":
            if not done_has_error_sentence(txt):
                return bot.send_message(uid, "Чуть конкретнее, пожалуйста (1–2 предложения). Что именно происходит?")
            save_error(uid, txt)
            data = {"error_text": txt, "mercedes": {}}
            set_state(uid, step="ask_mer_context", data=data)
            return bot.send_message(uid, ask_mercedes_block("context"), reply_markup=reply_kb())

        # (b) MERCEDES blocks
        order = mercedes_order()
        step_map = {
          "ask_mer_context": "context",
          "ask_mer_emotions": "emotions",
          "ask_mer_thoughts": "thoughts",
          "ask_mer_behavior": "behavior",
        }
        if st["step"] in step_map:
            block = step_map[st["step"]]
            data = st["data"] or {}
            mer = data.get("mercedes", {})
            mer[block] = txt
            data["mercedes"] = mer

            idx = order.index(block)
            if idx < len(order)-1:
                next_block = order[idx+1]
                next_step = {
                    "context":"ask_mer_emotions",
                    "emotions":"ask_mer_thoughts",
                    "thoughts":"ask_mer_behavior"
                }[block]
                set_state(uid, step=next_step, data=data)
                return bot.send_message(uid, ask_mercedes_block(next_block), reply_markup=reply_kb())
            else:
                # Сводка и переход к цели
                set_state(uid, step="ask_new_goal", data=data)
                mer = data["mercedes"]
                resume = (
                    f"*Резюме*\n"
                    f"Ошибка: {data.get('error_text')}\n"
                    f"Контекст: {mer.get('context','—')}\n"
                    f"Эмоции: {mer.get('emotions','—')}\n"
                    f"Мысли: {mer.get('thoughts','—')}\n"
                    f"Поведение: {mer.get('behavior','—')}\n\n"
                    f"Сформулируем новую цель одним предложением (что хочешь делать вместо прежнего поведения)?"
                )
                return bot.send_message(uid, resume, reply_markup=reply_kb())

        # (c) ask_new_goal → TOTE goal/ops/check/exit (MVP: только фиксация цели)
        if st["step"] == "ask_new_goal":
            if len(txt.strip()) < 5:
                return bot.send_message(uid, "Сформулируй цель одним предложением (понятно и наблюдаемо).")
            data = st["data"] or {}
            data["new_goal"] = txt.strip()
            set_state(uid, intent="greet", step="free_talk", data=data)
            return bot.send_message(uid, f"Отлично. Цель зафиксирована: *{txt.strip()}*.\nМожем добавить это в фокус недели позже. Чем ещё помочь сейчас?", reply_markup=reply_kb())

    # 3) Off-script fallback (коротко поддержим разговор)
    if is_smalltalk(txt):
        return bot.reply_to(m, "Привет! Готов помочь. О чём поговорим — про ошибку, стратегию или рынок?", reply_markup=reply_kb())
    return bot.reply_to(m, "Понял. Если хочешь, могу провести через короткий разбор ошибки — нажми «🚑 У меня ошибка».", reply_markup=reply_kb())

# ───────────────────────────
# Flask endpoints
# ───────────────────────────
@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

@app.get("/status")
def http_status():
    # без user_id это общий статус сервиса
    ok_db = True
    try:
        db_exec("SELECT 1")
    except Exception:
        ok_db = False
    return jsonify({
        "ok": ok_db,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "webhook": f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    })

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Безопасность периметра
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        abort(413)
    upd = request.get_data().decode("utf-8", errors="ignore")
    try:
        bot.process_new_updates([types.Update.de_json(upd)])
    except Exception as e:
        log.exception("process_new_updates error")
        abort(500)
    return "ok", 200

# ───────────────────────────
# Webhook setup on boot (optional)
# ───────────────────────────
def set_webhook():
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    ok = bot.set_webhook(
        url=url,
        secret_token=TG_WEBHOOK_SECRET,
        allowed_updates=["message","callback_query"],
        max_connections=40,
        drop_pending_updates=False
    )
    if ok:
        log.info("Webhook set to %s", url)
    else:
        log.error("Failed to set webhook to %s", url)

if __name__ == "__main__":
    ensure_schema()
    if ALLOW_SET_WEBHOOK:
        try:
            set_webhook()
        except Exception:
            log.exception("set_webhook failed")
    # Render запускает как веб-сервис — только Flask
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

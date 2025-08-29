import os, json, time, logging, traceback
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, abort, jsonify

import telebot
from telebot import types

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# ---------- ENV ----------
TOKEN               = os.getenv("TELEGRAM_TOKEN", "")
DATABASE_URL        = os.getenv("DATABASE_URL", "")
PUBLIC_URL          = os.getenv("PUBLIC_URL", "")                     # https://innertrade-bot.onrender.com
WEBHOOK_PATH        = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")
TG_WEBHOOK_SECRET   = os.getenv("TG_WEBHOOK_SECRET", "")
ALLOW_SET_WEBHOOK   = os.getenv("ALLOW_SET_WEBHOOK", "1") in ("1","true","True")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")
OFFSCRIPT_ENABLED   = os.getenv("OFFSCRIPT_ENABLED", "1") in ("1","true","True")

OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL        = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ---------- LOG ----------
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
log = logging.getLogger("innertrade")

# ---------- GUARDS ----------
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")

# ---------- DB ----------
engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, future=True)

DDL = """
CREATE TABLE IF NOT EXISTS users (
  user_id     BIGINT PRIMARY KEY,
  mode        TEXT NOT NULL DEFAULT 'course',
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_state (
  user_id     BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  intent      TEXT,
  step        TEXT,
  data        JSONB,
  updated_at  TIMESTAMPTZ DEFAULT now()
);
"""

def db_init():
    with engine.begin() as conn:
        conn.execute(text(DDL))

def save_state(uid: int, intent: str=None, step: str=None, merge_data: dict=None):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(user_id) VALUES (:uid) ON CONFLICT (user_id) DO NOTHING"), {"uid": uid})
        # fetch existing state
        row = conn.execute(text("SELECT data FROM user_state WHERE user_id=:uid"), {"uid": uid}).fetchone()
        cur = row[0] if row and row[0] else {}
        if merge_data:
            cur.update(merge_data)
        conn.execute(text("""
            INSERT INTO user_state(user_id, intent, step, data, updated_at)
            VALUES (:uid, :intent, :step, CAST(:data AS JSONB), now())
            ON CONFLICT (user_id) DO UPDATE SET
              intent = COALESCE(EXCLUDED.intent, user_state.intent),
              step   = COALESCE(EXCLUDED.step,   user_state.step),
              data   = COALESCE(EXCLUDED.data,   user_state.data),
              updated_at = now()
        """), {"uid": uid, "intent": intent, "step": step, "data": json.dumps(cur)})

def get_state(uid: int):
    with engine.begin() as conn:
        row = conn.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:uid"), {"uid": uid}).fetchone()
    if not row:
        return {"intent": None, "step": None, "data": {}}
    return {"intent": row[0], "step": row[1], "data": row[2] or {}}

# ---------- OPENAI (off-script) ----------
def gpt_reply(style_you: str, user_text: str, context_hint: str):
    """
    Лёгкий оффскрипт-ответ. Если нет ключа — возвращаем None.
    """
    if not (OFFSCRIPT_ENABLED and OPENAI_API_KEY):
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        system_prompt = (
            "Ты наставник по трейдингу. Отвечай коротко, спокойно и по делу, "
            "поддерживай тон беседы и возвращай к цели разговора. "
            f"Обращайся на «{style_you}». Избегай профессионального жаргона. "
            "В конце мягко предложи вернуться к шагам, если это уместно."
        )
        msg = [
            {"role":"system","content": system_prompt},
            {"role":"user","content": f"Контекст: {context_hint}\nСообщение: {user_text}"}
        ]
        r = client.chat.completions.create(model=OPENAI_MODEL, messages=msg, temperature=0.5, max_tokens=180)
        return r.choices[0].message.content.strip()
    except Exception as e:
        log.warning("gpt_reply failed: %s", e)
        return None

# ---------- TELEGRAM ----------
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)

MAIN_KB = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
MAIN_KB.add(
    types.KeyboardButton("🚑 У меня ошибка"),
    types.KeyboardButton("🧩 Хочу стратегию"),
    types.KeyboardButton("📄 Паспорт"),
    types.KeyboardButton("🗒 Панель недели"),
    types.KeyboardButton("🆘 Экстренно: поплыл"),
    types.KeyboardButton("🤔 Не знаю, с чего начать"),
)

def greet(uid: int, first_name: str):
    save_state(uid, intent="greet", step="ask_form", merge_data={"name": first_name, "you": None})
    return ("👋 Привет! Можем просто поговорить — напиши, что болит в торговле.\n"
            "Или выбери пункт ниже.\n\n"
            "Как удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)")

def confirm_you(uid: int, you: str):
    you = you.lower().strip()
    you = "ты" if you.startswith("т") else "вы"
    save_state(uid, intent="greet", step="free_talk", merge_data={"you": you, "free_turns": 0})
    return f"Принято ({you}). Можем просто поговорить — расскажи, что сейчас болит, или выбери пункт ниже."

def want_error_flow(uid: int):
    save_state(uid, intent="error", step="ask_error", merge_data={"mercedes": {}})
    return ("Опиши основную ошибку 1–2 предложениями.\n"
            "Например: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю на первой коррекции».")

def ask_mercedes(uid: int, block: str):
    prompts = {
        "context": "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
        "emotions": "ЭМОЦИИ. Что чувствуешь в момент ошибки? (несколько слов)",
        "thoughts": "МЫСЛИ. Что говоришь себе в этот момент? (1–2 фразы)",
        "behavior": "ПОВЕДЕНИЕ. Что именно ты делаешь? Опиши действие глаголами (1–2 предложения)",
    }
    save_state(uid, step=f"ask_{block}")
    return prompts[block]

def mercedes_summary(m):
    d = m.get("mercedes", {})
    ctx = d.get("context") or "—"
    emo = d.get("emotions") or "—"
    tho = d.get("thoughts") or "—"
    beh = d.get("behavior") or "—"
    return (f"<b>Резюме</b>:\n"
            f"Контекст: {ctx}\n"
            f"Эмоции: {emo}\n"
            f"Мысли: {tho}\n"
            f"Поведение: {beh}")

def ask_new_goal(uid: int):
    save_state(uid, step="ask_new_goal")
    return "Сформулируем новую цель одним коротким предложением (что хочешь делать вместо прежнего поведения)?"

def ask_tote(uid: int, which: str):
    labels = {
        "goal":  "TOTE — Цель (Test 1). Как сформулируем желаемый результат на ближайшие 3 сделки?",
        "ops":   "TOTE — Действия (Operate). Какие 2–3 шага помогут держаться этой цели?",
        "check": "TOTE — Проверка (Test 2). Как поймёшь, что получилось? (критерии)",
        "exit":  "TOTE — Выход (Exit). Что усилим/исправим по итогу?"
    }
    save_state(uid, step=f"ask_tote_{which}")
    return labels[which]

def finalize_error(uid: int, data: dict):
    save_state(uid, intent="idle", step=None)
    return ("Готово. Записал краткий план.\n"
            "Готов продолжить, если нужно: можно добавить это в недельный фокус или перейти к стратегии.")

# ---------- COMMANDS ----------
@bot.message_handler(commands=['start','reset'])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    msg = greet(uid, m.from_user.first_name or "друг")
    bot.send_message(uid, msg, reply_markup=MAIN_KB)

@bot.message_handler(commands=['ping'])
def cmd_ping(m: types.Message):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=['status'])
def cmd_status(m: types.Message):
    uid = m.from_user.id
    st = get_state(uid)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {
        "ok": True,
        "time": now,
        "intent": st.get("intent"),
        "step": st.get("step"),
        "db": "ok"
    }
    bot.send_message(uid, f"<pre>{json.dumps(out, ensure_ascii=False, indent=2)}</pre>")

# ---------- MENU/INTENTS ----------
@bot.message_handler(func=lambda m: m.text in (
    "🚑 У меня ошибка","🧩 Хочу стратегию","📄 Паспорт","🗒 Панель недели","🆘 Экстренно: поплыл","🤔 Не знаю, с чего начать"))
def menu_click(m: types.Message):
    uid = m.from_user.id
    txt = m.text
    if txt == "🚑 У меня ошибка":
        bot.send_message(uid, want_error_flow(uid))
    elif txt == "🧩 Хочу стратегию":
        save_state(uid, intent="strategy", step="intro")
        bot.send_message(uid, "Ок, соберем ТС базово в 2 шага: 1) вход/подход/ТФ 2) стоп/сопровождение/лимиты.\nГотов?")
    elif txt == "📄 Паспорт":
        save_state(uid, intent="passport", step="intro")
        bot.send_message(uid, "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?")
    elif txt == "🗒 Панель недели":
        save_state(uid, intent="week_panel", step="focus")
        bot.send_message(uid, "Панель недели: 1) фокус недели 2) 1–2 цели 3) лимиты 4) ритуалы.\nНачнем с фокуса?")
    elif txt == "🆘 Экстренно: поплыл":
        save_state(uid, intent="sos", step="protocol")
        bot.send_message(uid, "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану или закрой по правилу")
    elif txt == "🤔 Не знаю, с чего начать":
        save_state(uid, intent="route", step="suggest")
        bot.send_message(uid, "Предлагаю так: 1) Паспорт 2) Фокус недели 3) Скелет ТС.\nС чего начнем?")

# ---------- TEXT FLOW ----------
@bot.message_handler(func=lambda m: True, content_types=['text'])
def on_text(m: types.Message):
    uid = m.from_user.id
    text_in = (m.text or "").strip()
    st = get_state(uid)
    intent = st.get("intent")
    step = st.get("step") or ""
    data = st.get("data") or {}
    you = data.get("you")  # "ты" | "вы" | None

    # 0) первичное согласование "ты/вы"
    if intent in (None, "greet") and (step in (None, "ask_form")):
        t = text_in.lower()
        if "ты" in t or "вы" in t:
            bot.send_message(uid, confirm_you(uid, t))
            return
        # оффскрипт-подсказка
        reply = gpt_reply(you or "ты", text_in, "первый контакт, пользователь ещё не выбрал ты/вы")
        hint = "\n\nНапиши, пожалуйста, <b>«ты»</b> или <b>«вы»</b>."
        bot.send_message(uid, (reply or "Ок, понял.") + hint, reply_markup=MAIN_KB)
        return

    # 1) свободное общение 1–3 реплики → мягкий переход
    if (intent == "greet" and step == "free_talk"):
        turns = int(data.get("free_turns") or 0) + 1
        save_state(uid, merge_data={"free_turns": turns})
        reply = gpt_reply(you or "ты", text_in, "свободный диалог до перехода к структуре")
        if turns >= 2:
            tail = "\n\nЕсли готов — нажми «🚑 У меня ошибка», и пройдёмся коротко по шагам."
        else:
            tail = "\n\nРасскажи ещё чуть-чуть, я слушаю."
        bot.send_message(uid, (reply or "Понимаю.") + tail, reply_markup=MAIN_KB)
        return

    # 2) поток "Ошибка" (MERCEDES → цель → TOTE)
    if intent == "error":
        mer = data.get("mercedes") or {}
        # done-условия и шаги:
        if step == "ask_error" or step is None:
            # пытаемся распознать слишком абстрактно?
            if len(text_in) < 5:
                bot.send_message(uid, "Опиши, пожалуйста, чуть конкретнее (1–2 предложения).")
                return
            mer["error"] = text_in
            save_state(uid, step="ask_context", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_mercedes(uid, "context"))
            return

        if step == "ask_context":
            mer["context"] = text_in
            save_state(uid, step="ask_emotions", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_mercedes(uid, "emotions"))
            return

        if step == "ask_emotions":
            mer["emotions"] = text_in
            save_state(uid, step="ask_thoughts", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_mercedes(uid, "thoughts"))
            return

        if step == "ask_thoughts":
            mer["thoughts"] = text_in
            save_state(uid, step="ask_behavior", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_mercedes(uid, "behavior"))
            return

        if step == "ask_behavior":
            mer["behavior"] = text_in
            save_state(uid, step="confirm_summary", merge_data={"mercedes": mer})
            bot.send_message(uid, mercedes_summary(data))
            bot.send_message(uid, "Так и зафиксировать? Напиши «да» или уточни.")
            return

        if step == "confirm_summary":
            if text_in.lower().startswith("д"):
                bot.send_message(uid, ask_new_goal(uid))
            else:
                # оффскрипт: коротко перефразуем и уточним
                reply = gpt_reply(you or "ты", text_in, "уточнение резюме MERCEDES; попроси уточнить 1 деталь")
                bot.send_message(uid, (reply or "Что уточним в резюме?"))
            return

        if step == "ask_new_goal":
            if len(text_in) < 5:
                bot.send_message(uid, "Сформулируй цель одним коротким предложением, пожалуйста.")
                return
            mer["new_goal"] = text_in
            save_state(uid, step="ask_tote_goal", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_tote(uid, "goal"))
            return

        if step == "ask_tote_goal":
            mer["tote_goal"] = text_in
            save_state(uid, step="ask_tote_ops", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_tote(uid, "ops"))
            return

        if step == "ask_tote_ops":
            if text_in.lower() in ("не знаю","не знаю.","я не знаю","нет"):
                # предложим 3 мягкие заготовки
                bot.send_message(uid, "Можно так: 1) чек-лист перед входом; 2) 2 минуты пауза/дыхание; 3) не трогаю стоп/тейк до условия.")
            mer["tote_ops"] = text_in
            save_state(uid, step="ask_tote_check", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_tote(uid, "check"))
            return

        if step == "ask_tote_check":
            mer["tote_check"] = text_in
            save_state(uid, step="ask_tote_exit", merge_data={"mercedes": mer})
            bot.send_message(uid, ask_tote(uid, "exit"))
            return

        if step == "ask_tote_exit":
            mer["tote_exit"] = text_in
            save_state(uid, merge_data={"mercedes": mer})
            bot.send_message(uid, finalize_error(uid, data))
            bot.send_message(uid, "Если хочешь — добавим это в «Панель недели» или перейдём к ТС.", reply_markup=MAIN_KB)
            return

    # 3) fallback: оффскрипт → мягкий возврат
    reply = gpt_reply(you or "ты" if you else "ты", text_in, f"intent={intent}, step={step}")
    if reply:
        bot.send_message(uid, reply, reply_markup=MAIN_KB)
    else:
        bot.send_message(uid, "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /reset.", reply_markup=MAIN_KB)

# ---------- FLASK ----------
app = Flask(__name__)

def require_secret(fn):
    @wraps(fn)
    def _wrap(*args, **kwargs):
        if TG_WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
            abort(401)
        return fn(*args, **kwargs)
    return _wrap

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.now(timezone.utc).isoformat()})

@app.get("/")
def root():
    return "OK"

@app.get("/status")
def status_http():
    return jsonify({"ok":True,"time":datetime.now(timezone.utc).isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
@require_secret
def webhook():
    try:
        if request.content_length and request.content_length > 1_000_000:
            abort(413)
        update = request.get_data().decode("utf-8")
        bot.process_new_updates([telebot.types.Update.de_json(update)])
        return "OK"
    except Exception as e:
        log.error("webhook error: %s\n%s", e, traceback.format_exc())
        return ("", 500)

def ensure_webhook():
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    if not ALLOW_SET_WEBHOOK:
        log.info("Skip set_webhook (ALLOW_SET_WEBHOOK=0)")
        return
    # set webhook with secret
    bot.remove_webhook()
    time.sleep(0.5)
    ok = bot.set_webhook(url=url, secret_token=TG_WEBHOOK_SECRET, allowed_updates=["message","callback_query"])
    log.info("set_webhook(%s) → %s", url, ok)

if __name__ == "__main__":
    db_init()
    ensure_webhook()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

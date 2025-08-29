import os, json, time, logging, datetime as dt
from contextlib import contextmanager

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
import requests

from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

# =========================
# ENV
# =========================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
DATABASE_URL     = os.getenv("DATABASE_URL", "").strip()
PUBLIC_URL       = os.getenv("PUBLIC_URL", "").strip()           # e.g. https://innertrade-bot.onrender.com
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "wbhk_9t3x").strip()
TG_WEBHOOK_SECRET= os.getenv("TG_WEBHOOK_SECRET", "").strip()
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OFFSCRIPT_ENABLED= os.getenv("OFFSCRIPT_ENABLED", "true").lower() in ("1","true","yes")
ALLOW_SET_WEBHOOK= os.getenv("ALLOW_SET_WEBHOOK", "0").lower() in ("1","true","yes")
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not WEBHOOK_PATH:
    raise RuntimeError("WEBHOOK_PATH missing")
if not TG_WEBHOOK_SECRET:
    raise RuntimeError("TG_WEBHOOK_SECRET missing")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("innertrade")

# =========================
# APP & DB
# =========================
app = Flask(__name__)
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=False)

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    future=True,
)

@contextmanager
def db():
    with engine.begin() as conn:
        yield conn

def ensure_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS users(
      user_id    BIGINT PRIMARY KEY,
      mode       TEXT NOT NULL DEFAULT 'course',
      created_at TIMESTAMPTZ DEFAULT now(),
      updated_at TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS user_state(
      user_id    BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
      intent     TEXT,
      step       TEXT,
      data       JSONB,
      updated_at TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS errors(
      id                BIGSERIAL PRIMARY KEY,
      user_id           BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
      error_text        TEXT NOT NULL,
      pattern_behavior  TEXT,
      pattern_emotion   TEXT,
      pattern_thought   TEXT,
      positive_goal     TEXT,
      tote_goal         TEXT,
      tote_ops          TEXT,
      tote_check        TEXT,
      tote_exit         TEXT,
      checklist_pre     TEXT,
      checklist_post    TEXT,
      created_at        TIMESTAMPTZ DEFAULT now()
    );
    """
    with db() as conn:
        conn.exec_driver_sql(ddl)

def upsert_user(uid: int):
    with db() as conn:
        conn.execute(text("""
            INSERT INTO users(user_id) VALUES (:uid)
            ON CONFLICT (user_id) DO UPDATE SET updated_at = now()
        """), {"uid": uid})
        conn.execute(text("""
            INSERT INTO user_state(user_id, intent, step, data)
            VALUES (:uid, 'greet', 'ask_form', '{}'::jsonb)
            ON CONFLICT (user_id) DO NOTHING
        """), {"uid": uid})

def set_state(uid: int, intent: str = None, step: str = None, patch: dict | None = None):
    with db() as conn:
        row = conn.execute(text("SELECT data FROM user_state WHERE user_id=:uid"), {"uid": uid}).first()
        data = row[0] if row and row[0] else {}
        if patch:
            data.update(patch)
        conn.execute(text("""
            UPDATE user_state
            SET intent = COALESCE(:intent, intent),
                step   = COALESCE(:step, step),
                data   = :data,
                updated_at = now()
            WHERE user_id=:uid
        """), {"uid": uid, "intent": intent, "step": step, "data": json.dumps(data)})

def get_state(uid: int):
    with db() as conn:
        r = conn.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:uid"), {"uid": uid}).first()
        if not r:
            return None
        intent, step, data = r
        return {"intent": intent, "step": step, "data": data or {}}

def save_error(uid: int, error_text: str):
    with db() as conn:
        conn.execute(text("""
            INSERT INTO errors(user_id, error_text) VALUES (:uid, :et)
        """), {"uid": uid, "et": error_text})

# =========================
# HELPERS
# =========================
MAIN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_MENU.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
MAIN_MENU.row("📄 Паспорт", "🗒 Панель недели")
MAIN_MENU.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")

PRONOUN_MENU = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
PRONOUN_MENU.row("ты", "вы")

def looks_like_behavioral_problem(text_: str) -> bool:
    t = (text_ or "").lower()
    verbs = ["вхожу","захожу","закрываю","двигаю","переношу","усредняю","снимаю","стоп","тейк","фиксирую"]
    ok_len = len(t) >= 20
    hit = any(v in t for v in verbs)
    return ok_len and hit

def summarize_problem_with_gpt(history: list[str]) -> str | None:
    if not (OPENAI_API_KEY and OFFSCRIPT_ENABLED):
        return None
    try:
        import openai
        from openai import OpenAI
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        client = OpenAI()
        prompt = (
            "Кратко (одним предложением) сформулируй торговую проблему пользователя "
            "на уровне поведения/навыка (без диагнозов, ценностей и теории). Примеры: "
            "«вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю на первой коррекции».\n\n"
            "Диалог:\n" + "\n".join(history[-10:])
        )
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content":"Ты наставник-трейдинг коуч. Пиши коротко и по делу."},
                {"role":"user","content":prompt}
            ],
            temperature=0.2,
            max_tokens=60,
        )
        cand = completion.choices[0].message.content.strip()
        return cand
    except Exception as e:
        log.warning(f"OpenAI summarize failed: {e}")
        return None

def assistant_reply_free(uid: int, msg: str) -> str:
    """
    Если оффскрипт разрешён — даём тёплый короткий ответ и мягко ведём к фиксации.
    Если нет — короткий коучинг и предложение нажать кнопку.
    """
    st = get_state(uid) or {}
    data = st.get("data", {})
    history = data.get("chat_history", [])
    history.append(f"user: {msg}")
    data["chat_history"] = history[-20:]

    # Если в сообщении уже явная поведенческая ошибка — предложим фиксацию
    proposed = None
    if looks_like_behavioral_problem(msg):
        proposed = msg.strip()
    else:
        # попробуем GPT кратко сформулировать
        proposed = summarize_problem_with_gpt(history) if OFFSCRIPT_ENABLED else None

    if proposed:
        set_state(uid, patch={"proposed_problem": proposed, "chat_history": history})
        return (
            f"Понял. Зафиксирую так: <b>{proposed}</b>\n"
            "Подходит? Нажми одну из кнопок ниже.",
        )
    else:
        set_state(uid, patch={"chat_history": history})
        if OFFSCRIPT_ENABLED and OPENAI_API_KEY:
            # лёгкое сочувствие + уточнение
            return (
                "Понимаю. Можем разобраться — опиши, пожалуйста, в 1–2 предложениях, "
                "что именно делаешь (действиями), когда происходит ошибка."
            )
        return (
            "Понимаю. Чтобы не потеряться, давай опишем коротко (1–2 предложения), "
            "что именно ты делаешь, когда случается ошибка. Потом быстро разберём по шагам."
        )

def build_confirm_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Да, так и есть", callback_data="confirm_problem_yes"),
        types.InlineKeyboardButton("Нет, хочу уточнить", callback_data="confirm_problem_no"),
    )
    return kb

def mercedes_question(step_key: str) -> str:
    mapping = {
        "ctx": "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
        "emo": "ЭМОЦИИ. Что чувствуешь в этот момент? (несколько слов)",
        "thought": "МЫСЛИ. Что говоришь себе в этот момент? (1–2 короткие фразы)",
        "behavior": "ПОВЕДЕНИЕ. Что ты делаешь конкретно? Опиши действия (1–2 предложения).",
    }
    return mapping.get(step_key, "Продолжим.")

def next_mercedes_step(current: str | None) -> str | None:
    order = [None, "ctx", "emo", "thought", "behavior"]
    try:
        idx = order.index(current)
    except ValueError:
        idx = 0
    return order[idx + 1] if idx + 1 < len(order) else None

def start_mercedes(uid: int, problem: str):
    set_state(uid, intent="error_flow", step="m_ctx", patch={
        "problem": problem,
        "mer": {"ctx": None, "emo": None, "thought": None, "behavior": None}
    })

def mercedes_save(uid: int, key: str, value: str):
    st = get_state(uid)
    mer = st["data"].get("mer", {})
    mer[key] = value
    set_state(uid, patch={"mer": mer})

def mercedes_complete(uid: int) -> dict:
    st = get_state(uid)
    data = st["data"]
    return {
        "problem": data.get("problem"),
        "ctx": data.get("mer", {}).get("ctx"),
        "emo": data.get("mer", {}).get("emo"),
        "thought": data.get("mer", {}).get("thought"),
        "behavior": data.get("mer", {}).get("behavior"),
    }

# =========================
# WEB
# =========================
@app.get("/health")
def health():
    return jsonify({"status":"ok","time":dt.datetime.utcnow().isoformat()})

@app.get("/status")
def status():
    try:
        with db() as conn:
            conn.exec_driver_sql("SELECT 1")
        return jsonify({"ok": True, "time": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00"), "db":"ok"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post(f"/webhook/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_data().decode("utf-8")
    bot.process_new_updates([types.Update.de_json(update)])
    return "OK"

# =========================
# BOT COMMANDS
# =========================
@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.reply_to(m, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    upsert_user(m.from_user.id)
    st = get_state(m.from_user.id)
    payload = {
        "ok": True,
        "time": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "intent": (st or {}).get("intent"),
        "step": (st or {}).get("step"),
        "db": "ok"
    }
    bot.reply_to(m, f"<code>{json.dumps(payload, ensure_ascii=False, indent=2)}</code>")

@bot.message_handler(commands=["reset","start"])
def cmd_reset(m):
    uid = m.from_user.id
    upsert_user(uid)
    set_state(uid, intent="greet", step="ask_form", patch={"proposed_problem": None, "problem": None, "mer": {}})
    bot.send_message(uid,
        f"👋 Привет, {m.from_user.first_name or ''}!\n"
        "Можем просто поговорить — напиши, что болит в торговле. Или выбери пункт ниже.\n\n"
        "Как удобнее обращаться — <b>ты</b> или <b>вы</b>? (напиши одно слово)",
        reply_markup=PRONOUN_MENU
    )

# =========================
# BOT: BUTTONS
# =========================
@bot.message_handler(func=lambda m: m.text in ("ты","вы"))
def set_addressing(m):
    uid = m.from_user.id
    upsert_user(uid)
    set_state(uid, patch={"addressing": m.text})
    bot.send_message(uid, "Принято. Можем спокойно поговорить — расскажи, что сейчас болит, или выбери пункт ниже.", reply_markup=MAIN_MENU)

@bot.message_handler(func=lambda m: m.text == "🚑 У меня ошибка")
def intent_error(m):
    uid = m.from_user.id
    upsert_user(uid)
    st = get_state(uid)
    data = st.get("data", {})
    # Если уже есть предложенная/зафиксированная формулировка — не переспрашиваем
    problem = data.get("problem") or data.get("proposed_problem")
    if problem and looks_like_behavioral_problem(problem):
        start_mercedes(uid, problem)
        bot.send_message(uid, f"Ок. Разберём коротко. Ошибка: <b>{problem}</b>\n\n" + mercedes_question("ctx"))
        return
    set_state(uid, intent="error_flow", step="ask_problem")
    bot.send_message(uid,
        "Опиши основную ошибку 1–2 предложениями (что именно ты ДЕЛАЕШЬ, когда она случается).\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю на первой коррекции».",
        reply_markup=types.ReplyKeyboardRemove()
    )

# =========================
# BOT: CALLBACKS
# =========================
@bot.callback_query_handler(func=lambda c: c.data in ("confirm_problem_yes","confirm_problem_no"))
def cb_confirm_problem(c):
    uid = c.from_user.id
    st = get_state(uid)
    proposed = (st or {}).get("data", {}).get("proposed_problem")
    if c.data == "confirm_problem_yes" and proposed:
        set_state(uid, patch={"problem": proposed})
        start_mercedes(uid, proposed)
        bot.answer_callback_query(c.id, "Зафиксировали.")
        bot.send_message(uid, f"Идём дальше. Ошибка: <b>{proposed}</b>\n\n" + mercedes_question("ctx"))
    else:
        set_state(uid, step="ask_problem", patch={"proposed_problem": None})
        bot.answer_callback_query(c.id, "Хорошо, уточним.")
        bot.send_message(uid, "Тогда сформулируй ошибку по-другому (1–2 предложения о ДЕЙСТВИЯХ).")

# =========================
# BOT: TEXT FLOW
# =========================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    text_in = (m.text or "").strip()
    upsert_user(uid)
    st = get_state(uid) or {"intent":"greet","step":"ask_form","data":{}}
    intent = st["intent"]
    step   = st["step"]
    data   = st.get("data", {})

    # 1) Если ждём ошибку (ask_problem)
    if intent == "error_flow" and step == "ask_problem":
        if looks_like_behavioral_problem(text_in):
            set_state(uid, patch={"problem": text_in})
            start_mercedes(uid, text_in)
            bot.send_message(uid, f"Ок. Ошибка: <b>{text_in}</b>\n\n" + mercedes_question("ctx"))
        else:
            # попробуем оффскрипт-обобщение
            proposed_block = summarize_problem_with_gpt([f"user: {text_in}"]) if OFFSCRIPT_ENABLED else None
            if proposed_block:
                set_state(uid, patch={"proposed_problem": proposed_block})
                bot.send_message(uid, f"Понял. Зафиксирую так: <b>{proposed_block}</b>\nПодходит?",
                                 reply_markup=build_confirm_kb())
            else:
                bot.send_message(uid, "Немного конкретнее про ДЕЙСТВИЯ: что именно делаешь? (пример: «двигаю стоп сразу после входа»)")
        return

    # 2) MERCEDES шаги
    if intent == "error_flow" and step and step.startswith("m_"):
        key = step.split("_", 1)[1]  # ctx/emo/thought/behavior
        mercedes_save(uid, key, text_in)
        nxt_key = next_mercedes_step(key)
        if nxt_key:
            set_state(uid, step=f"m_{nxt_key}")
            bot.send_message(uid, mercedes_question(nxt_key))
            return
        # завершили MERCEDES
        snap = mercedes_complete(uid)
        # сохраним запись ошибки (минимум)
        save_error(uid, snap["problem"] or "")
        # короткое резюме и переход к цели (TOTE Goal light)
        summary = (
            f"Резюме:\n"
            f"• Ошибка: {snap['problem']}\n"
            f"• Контекст: {snap['ctx']}\n"
            f"• Эмоции: {snap['emo']}\n"
            f"• Мысли: {snap['thought']}\n"
            f"• Поведение: {snap['behavior']}\n\n"
            "Сформулируем новую цель одним предложением: что хочешь делать вместо прежнего поведения?"
        )
        set_state(uid, step="tote_goal")
        bot.send_message(uid, summary)
        return

    # 3) TOTE goal
    if intent == "error_flow" and step == "tote_goal":
        goal = text_in
        set_state(uid, step="tote_ops", patch={"tote_goal": goal})
        bot.send_message(uid, "Ок. Какие 2–3 шага помогут держаться этой цели в ближайших 3 сделках?")
        return

    # 4) TOTE ops
    if intent == "error_flow" and step == "tote_ops":
        ops = text_in
        set_state(uid, step="tote_done", patch={"tote_ops": ops})
        st2 = get_state(uid)
        goal = st2["data"].get("tote_goal","")
        bot.send_message(uid,
            f"Готово.\n<b>Цель:</b> {goal}\n<b>Шаги:</b> {ops}\n\n"
            "Добавлю в план недели при желании. Можем вернуться в меню.",
            reply_markup=MAIN_MENU
        )
        # финал урока 1 — вернёмся в обычный режим
        set_state(uid, intent="greet", step="ask_form")
        return

    # 5) Иначе — оффскрипт или мягкая подводка
    reply = assistant_reply_free(uid, text_in)
    if isinstance(reply, tuple):
        bot.send_message(uid, reply[0], reply_markup=build_confirm_kb())
    else:
        bot.send_message(uid, reply)

# =========================
# STARTUP
# =========================
def set_webhook():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    target = f"{PUBLIC_URL}/webhook/{WEBHOOK_PATH}"
    payload = {
        "url": target,
        "secret_token": TG_WEBHOOK_SECRET,
        "allowed_updates": ["message","callback_query"],
        "drop_pending_updates": True
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        log.info(f"setWebhook -> {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"setWebhook failed: {e}")

def del_webhook():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", timeout=10)
        log.info(f"deleteWebhook -> {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"deleteWebhook failed: {e}")

def startup():
    ensure_schema()
    if ALLOW_SET_WEBHOOK:
        del_webhook()
        set_webhook()
    log.info("Ready.")

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

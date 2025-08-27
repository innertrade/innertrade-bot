import os, logging, json, threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from openai import OpenAI

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- ENV ----------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
DATABASE_URL     = os.getenv("DATABASE_URL")
PUBLIC_URL       = os.getenv("PUBLIC_URL")          # e.g. https://innertrade-bot.onrender.com
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "wbhk")  # e.g. wbhk_9t3x
TG_WEBHOOK_SECRET= os.getenv("TG_WEBHOOK_SECRET")     # arbitrary UUID-like

if not TELEGRAM_TOKEN:    raise RuntimeError("TELEGRAM_TOKEN is missing")
if not OPENAI_API_KEY:    raise RuntimeError("OPENAI_API_KEY is missing")
if not PUBLIC_URL:        raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if not TG_WEBHOOK_SECRET: raise RuntimeError("TG_WEBHOOK_SECRET missing")

# ---------- OPENAI ----------
client = OpenAI(api_key=OPENAI_API_KEY)

def ai_coach_reply(prompt: str) -> str:
    """
    Короткий, тёплый ответ, чтобы поддержать пользователя и мягко вернуть к вопросу.
    Если что-то пойдёт не так — вернём нейтральную фразу.
    """
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"Ты эмпатичный коуч по трейдингу. Отвечай кратко (1-2 предложения), поддерживай и возвращай к вопросу."},
                {"role":"user","content":prompt}
            ],
            temperature=0.5,
            max_tokens=120
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"OpenAI error: {e}")
        return "Понимаю. Давай вернёмся к вопросу, чтобы двигаться дальше."

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
              user_id    BIGINT PRIMARY KEY,
              mode       TEXT NOT NULL DEFAULT 'course',
              created_at TIMESTAMPTZ DEFAULT now(),
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
              user_id    BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
              intent     TEXT,
              step       TEXT,
              data       JSONB,
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS errors (
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
            """))
        logging.info("DB connected & migrated")
    except OperationalError as e:
        logging.warning(f"DB not available: {e}")
        engine = None
else:
    logging.info("DATABASE_URL not set — running without DB persistence (ephemeral)")

def db_exec(sql: str, params: Optional[Dict[str,Any]] = None):
    if not engine: return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_user(user_id: int):
    db_exec("INSERT INTO users(user_id) VALUES (:u) ON CONFLICT (user_id) DO NOTHING", {"u": user_id})

def load_state(user_id: int) -> Dict[str,Any]:
    if not engine: return {"intent":"idle","step":None,"data":{}}
    row = db_exec("SELECT intent, step, COALESCE(data,'{}'::jsonb) AS data FROM user_state WHERE user_id=:u", {"u":user_id}).fetchone()
    if not row:
        save_state(user_id, "idle", None, {})
        return {"intent":"idle","step":None,"data":{}}
    return {"intent":row.intent, "step":row.step, "data":row.data}

def save_state(user_id: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[dict]=None):
    if not engine: return
    # merge strategy
    current = load_state(user_id)
    new_intent = intent if intent is not None else current.get("intent")
    new_step   = step   if step   is not None else current.get("step")
    new_data   = current.get("data", {})
    if data:
        # shallow merge
        new_data.update(data)
    db_exec("""
    INSERT INTO user_state(user_id, intent, step, data, updated_at)
    VALUES (:u, :i, :s, CAST(:d AS jsonb), now())
    ON CONFLICT (user_id) DO UPDATE
    SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"u":user_id, "i":new_intent, "s":new_step, "d":json.dumps(new_data)})

def store_error_record(user_id: int, payload: Dict[str,Any]):
    if not engine: return
    db_exec("""
    INSERT INTO errors(user_id, error_text, pattern_behavior, pattern_emotion, pattern_thought,
                       positive_goal, tote_goal, tote_ops, tote_check, tote_exit, checklist_pre, checklist_post)
    VALUES (:uid, :err, :pb, :pe, :pt, :goal, :tgoal, :tops, :tchk, :texit, :chkpre, :chkpost)
    """, {
        "uid": user_id,
        "err": payload.get("error_text",""),
        "pb":  payload.get("pattern_behavior",""),
        "pe":  payload.get("pattern_emotion",""),
        "pt":  payload.get("pattern_thought",""),
        "goal":payload.get("positive_goal",""),
        "tgoal":payload.get("tote_goal",""),
        "tops": payload.get("tote_ops",""),
        "tchk": payload.get("tote_check",""),
        "texit":payload.get("tote_exit",""),
        "chkpre":payload.get("checklist_pre",""),
        "chkpost":payload.get("checklist_post",""),
    })

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# ---------- ВСПОМОГАТЕЛЬНАЯ ЛОГИКА ДИАЛОГА ----------
CLARIFIERS = [
    "Когда это случается чаще всего? (дни/ситуации)",
    "Что запускает ошибку? (триггер/мысль/событие)",
    "Как выглядит действие на уровне поведения? (глаголами)"
]

MERCEDES_STEPS = [
    ("context", "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)"),
    ("emotions","ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)"),
    ("thoughts","МЫСЛИ. Что говоришь себе в этот момент? (1–2 коротких цитаты)"),
    ("behavior","ПОВЕДЕНИЕ. Что ты конкретно делаешь? (глаголами, 1–2 предложения)"),
    ("beliefs","УБЕЖДЕНИЯ/ЦЕННОСТИ. Почему «кажется правильным» так поступать?"),
    ("state","СОСТОЯНИЕ. В каком состоянии обычно входишь? (тревога/спешка/контроль и т.п.)")
]

TOTE_STEPS = [
    ("t_goal",  "TOTE — ЦЕЛЬ (Test). Сформулируй цель будущего поведения в 1 предложении (позитивно, наблюдаемо)."),
    ("t_ops",   "TOTE — ДЕЙСТВИЯ (Operate). Какие 2–3 шага помогут удержать цель?"),
    ("t_check", "TOTE — ПРОВЕРКА (Test). Как поймёшь, что цель удержана? (критерии)"),
    ("t_exit",  "TOTE — ВЫХОД (Exit). Если получилось — что закрепляем? Если нет — что меняем?")
]

def ask_next_mercedes(user_id: int, chat_id: int):
    st = load_state(user_id)
    data = st.get("data", {})
    mdat = data.get("mercedes", {})
    for key, question in MERCEDES_STEPS:
        if key not in mdat:
            save_state(user_id, step=f"mer_{key}", data=data)
            bot.send_message(chat_id, question, reply_markup=main_menu())
            return
    # всё собрано → резюме и переход к TOTE
    summary = (
        f"*Резюме MERCEDES*\n"
        f"- Контекст: {mdat.get('context','—')}\n"
        f"- Эмоции: {mdat.get('emotions','—')}\n"
        f"- Мысли: {mdat.get('thoughts','—')}\n"
        f"- Поведение: {mdat.get('behavior','—')}\n"
        f"- Убеждения/ценности: {mdat.get('beliefs','—')}\n"
        f"- Состояние: {mdat.get('state','—')}\n\n"
        "Перейдём к *TOTE*?"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Да", callback_data="go_tote"))
    bot.send_message(chat_id, summary, reply_markup=kb)

def ask_next_tote(user_id: int, chat_id: int):
    st = load_state(user_id)
    data = st.get("data", {})
    tdat = data.get("tote", {})
    for key, question in TOTE_STEPS:
        if key not in tdat:
            save_state(user_id, step=f"tote_{key}", data=data)
            bot.send_message(chat_id, question, reply_markup=main_menu())
            return
    # всё TOTE собрано → финализация записи
    payload = {
        "error_text": data.get("error_confirmed",""),
        "pattern_behavior": data.get("pattern_behavior",""),
        "pattern_emotion":  data.get("pattern_emotion",""),
        "pattern_thought":  data.get("pattern_thought",""),
        "positive_goal":    tdat.get("t_goal",""),
        "tote_goal":        tdat.get("t_goal",""),
        "tote_ops":         tdat.get("t_ops",""),
        "tote_check":       tdat.get("t_check",""),
        "tote_exit":        tdat.get("t_exit",""),
        "checklist_pre":    "Проверить сетап → пауза 10 вдохов → проговорить цель",
        "checklist_post":   "Фиксировать по плану → короткая запись итога"
    }
    store_error_record(user_id, payload)
    bot.send_message(chat_id, "Готово! Итог зафиксирован. Добавить это в фокус недели?", reply_markup=main_menu())
    save_state(user_id, intent="idle", step=None, data={})

# ---------- КОМАНДЫ ----------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="idle", step=None, data={})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*.\nМожем поговорить свободно или пойти по шагам.\nКоманды: /status /ping",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = load_state(m.from_user.id)
    intent = st.get("intent")
    step = st.get("step")
    data = st.get("data", {})
    parts = [f"*Статус*: живой ✅", f"*Intent*: {intent or '—'}", f"*Step*: {step or '—'}"]
    if data.get("error_raw"):
        parts.append(f"*Черновик ошибки*: {data.get('error_raw')}")
    if data.get("error_confirmed"):
        parts.append(f"*Ошибка (подтверждена)*: {data.get('error_confirmed')}")
    bot.send_message(m.chat.id, "\n".join(parts), reply_markup=main_menu())

# ---------- КНОПКИ МЕНЮ ----------
@bot.message_handler(func=lambda msg: msg.text=="🚑 У меня ошибка")
def intent_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="error", step="ask_problem", data={"clarifiers":[]})
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями *на уровне поведения/навыка*.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text=="🧩 Хочу стратегию")
def intent_strategy(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="strategy", step="intro")
    bot.send_message(
        m.chat.id,
        "Ок, соберём ТС по конструктору:\n1) Подход/ТФ/рынок\n2) Вход\n3) Стоп/сопровождение/выход\n4) Риск/лимиты\n(В этой версии активно делаем Урок 1. Стратегию подключим дальше.)",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text=="📄 Паспорт")
def intent_passport(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="passport", step="intro")
    bot.send_message(m.chat.id, "Паспорт трейдера: позже добавим редактирование профиля.\nПока можно продолжить с ошибкой.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🗒 Панель недели")
def intent_panel(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="week_panel", step="intro")
    bot.send_message(m.chat.id, "Панель недели: фокус, 1–2 цели, лимиты. Подключим после фиксации ошибки.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🆘 Экстренно: поплыл")
def intent_panic(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="panic", step="protocol")
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text=="🤔 Не знаю, с чего начать")
def intent_start_help(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="start_help", step="offer")
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Сформулируем и разберём текущую ошибку (MERCEDES+TOTE)\n2) Зафиксируем фокус недели\n3) Перейдём к скелету ТС",
        reply_markup=main_menu()
    )

# ---------- INLINE: подтверждение формулировки ошибки / переходы ----------
@bot.callback_query_handler(func=lambda c: c.data in ["err_confirm_yes","err_confirm_no","go_tote"])
def cb_confirm(c):
    uid = c.from_user.id
    st  = load_state(uid)
    data= st.get("data", {})
    if c.data == "err_confirm_yes":
        # зафиксировано → старт MERCEDES
        bot.answer_callback_query(c.id, "Зафиксировали. Пойдём по MERCEDES.")
        save_state(uid, intent="error", step="mer_context", data=data)
        ask_next_mercedes(uid, c.message.chat.id)
    elif c.data == "err_confirm_no":
        bot.answer_callback_query(c.id, "Ок, поправим формулировку. Напиши вариант точнее.")
        save_state(uid, step="clarify_fix", data=data)
        bot.send_message(c.message.chat.id, "Как бы ты сформулировал точнее? (1 предложение)")
    elif c.data == "go_tote":
        bot.answer_callback_query(c.id, "Переходим к TOTE")
        save_state(uid, step="tote_t_goal", data=data)
        ask_next_tote(uid, c.message.chat.id)

# ---------- ОБРАБОТКА ТЕКСТА ПО ШАГАМ ----------
@bot.message_handler(content_types=["text"])
def handle_text(m):
    uid = m.from_user.id
    ensure_user(uid)
    st   = load_state(uid)
    intent = st.get("intent","idle")
    step   = st.get("step")
    data   = st.get("data", {})

    txt = (m.text or "").strip()

    # 1) Сценарий "Ошибка" — сбор формулировки и уточнений
    if intent == "error" and step == "ask_problem":
        data["error_raw"] = txt
        data["clarifiers"] = []
        save_state(uid, step="clarify_1", data=data)
        bot.send_message(m.chat.id, f"Понял. {CLARIFIERS[0]}", reply_markup=main_menu())
        return

    if intent == "error" and (step or "").startswith("clarify_"):
        # пользователь мог задать вопрос вместо ответа
        if txt.endswith("?") and len(txt) < 200:
            reply = ai_coach_reply(f"Вопрос пользователя: {txt}")
            bot.send_message(m.chat.id, reply, reply_markup=main_menu())

        clar = data.get("clarifiers", [])
        clar.append(txt)
        data["clarifiers"] = clar

        idx = 1 if step=="clarify_1" else (2 if step=="clarify_2" else 99)

        if idx == 1:
            save_state(uid, step="clarify_2", data=data)
            bot.send_message(m.chat.id, CLARIFIERS[1], reply_markup=main_menu())
            return
        if idx == 2:
            # достаточно, синтез и подтверждение
            # попробуем вытащить «поведение/эмоции/мысли» хоть как-то
            behavior_hint = ""
            emotion_hint  = ""
            thought_hint  = ""

            # простые эвристики
            for line in clar:
                low = line.lower()
                if any(w in low for w in ["делаю","вхожу","открываю","закрываю","двигаю","перехожу","усредняю"]):
                    behavior_hint = line
                if any(w in low for w in ["тревог", "страх", "паник", "напряж", "спеш", "давлен"]):
                    emotion_hint = line
                if any(w in low for w in ["думаю","кажется","мысл","наверное","вдруг","успею","упущу"]):
                    thought_hint = line

            summary = f"Зафиксировать так: *{data.get('error_raw','') or '—'}*.\n\n" \
                      f"То, что ты описал(а):\n" \
                      f"• Контекст/триггеры: {clar[0] if len(clar)>0 else '—'}\n" \
                      f"• Дополнение: {clar[1] if len(clar)>1 else '—'}\n"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Да, так и есть", callback_data="err_confirm_yes"),
                   types.InlineKeyboardButton("Нет, поправлю", callback_data="err_confirm_no"))
            data["pattern_behavior"] = behavior_hint
            data["pattern_emotion"]  = emotion_hint
            data["pattern_thought"]  = thought_hint
            data["error_confirmed"]  = data.get("error_raw","")
            save_state(uid, step="wait_err_confirm", data=data)
            bot.send_message(m.chat.id, summary, reply_markup=kb)
            return

        # на всякий случай
        save_state(uid, step="clarify_2", data=data)
        bot.send_message(m.chat.id, CLARIFIERS[1], reply_markup=main_menu())
        return

    if intent == "error" and step == "clarify_fix":
        data["error_raw"] = txt
        data["clarifiers"] = []
        save_state(uid, step="clarify_1", data=data)
        bot.send_message(m.chat.id, CLARIFIERS[0], reply_markup=main_menu())
        return

    # 2) MERCEDES
    if intent == "error" and (step or "").startswith("mer_"):
        mdat = data.get("mercedes", {})
        key = step.replace("mer_","")
        mdat[key] = txt
        data["mercedes"] = mdat
        save_state(uid, data=data)
        ask_next_mercedes(uid, m.chat.id)
        return

    # 3) TOTE
    if intent == "error" and (step or "").startswith("tote_"):
        tdat = data.get("tote", {})
        key = step.replace("tote_","")
        tdat[key] = txt
        data["tote"] = tdat
        save_state(uid, data=data)
        ask_next_tote(uid, m.chat.id)
        return

    # 4) Общий свободный диалог (мягкая поддержка + возвращение к целям)
    if intent in ["idle","start_help","passport","week_panel","strategy","panic", None]:
        # краткий ответ ИИ, затем предложение действия
        reply = ai_coach_reply(f"Пользователь пишет: {txt}. Ответь поддерживающе и предложи вариант: разобрать текущую ошибку или задать цель на неделю.")
        bot.send_message(m.chat.id, reply, reply_markup=main_menu())
        if intent == "idle":
            bot.send_message(m.chat.id, "Если хочешь — нажми «🚑 У меня ошибка», разберём по шагам (MERCEDES → TOTE).", reply_markup=main_menu())
        return

    # Фолбэк
    bot.send_message(m.chat.id, "Принял. Давай продолжим. Если нужно — /menu", reply_markup=main_menu())

# ---------- FLASK / WEBHOOK ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK / Innertrade v7"

@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    # ограничение размера тела (≈1МБ)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_data().decode("utf-8")
    try:
        bot.process_new_updates([types.Update.de_json(json.loads(update))])
    except Exception as e:
        logging.exception(f"update error: {e}")
    return "OK"

def set_webhook():
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    ok = bot.set_webhook(url=url, secret_token=TG_WEBHOOK_SECRET, allowed_updates=["message","callback_query"])
    logging.info(f"Set webhook: {ok} url={url}")

def start_polling():
    # на всякий случай
    try:
        bot.remove_webhook()
    except Exception as e:
        logging.warning(f"remove webhook warn: {e}")
    logging.info("Starting polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)

if __name__ == "__main__":
    MODE = os.getenv("MODE", "webhook").lower()  # webhook | polling
    if MODE == "polling":
        t = threading.Thread(target=start_polling, daemon=True)
        t.start()
    else:
        # webhook-режим
        set_webhook()
    port = int(os.getenv("PORT","10000"))
    logging.info(f"Serving Flask on 0.0.0.0:{port} (mode={MODE})")
    app.run(host="0.0.0.0", port=port)

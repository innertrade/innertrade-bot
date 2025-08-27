# main.py — Innertrade (Render/Webhook)
import os, json, logging, re, threading
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- ENV ----------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")            # https://innertrade-bot.onrender.com
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH", "wbhk_9t3x")
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")
MODE               = os.getenv("MODE", "webhook")       # webhook | polling
ALLOW_GPT          = os.getenv("ALLOW_GPT", "1")        # "1" — GPT включён для off-script

if not TELEGRAM_TOKEN:  raise RuntimeError("TELEGRAM_TOKEN missing")
if not PUBLIC_URL:      raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")
if MODE not in ("webhook", "polling"): raise RuntimeError("MODE must be webhook|polling")

# ---------- OPENAI ----------
client = None
if OPENAI_API_KEY and ALLOW_GPT == "1":
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        logging.info("OpenAI client OK")
    except Exception as e:
        logging.warning(f"OpenAI init failed: {e}")

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        with engine.begin() as conn:
            # минимальные таблицы (без лишней «магии»)
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
    logging.info("No DATABASE_URL — running without DB")

def db_exec(sql: str, params: Optional[Dict[str, Any]] = None):
    if not engine: return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_user(uid: int):
    if not engine: return
    db_exec("INSERT INTO users(user_id) VALUES (:u) ON CONFLICT (user_id) DO NOTHING", {"u": uid})
    db_exec("""
        INSERT INTO user_state(user_id, intent, step, data)
        VALUES (:u, 'idle', NULL, '{}'::jsonb)
        ON CONFLICT (user_id) DO NOTHING
    """, {"u": uid})

def save_state(uid: int, intent: Optional[str] = None, step: Optional[str] = None, data: Optional[dict] = None):
    if not engine: return
    ensure_user(uid)
    cur = db_exec("SELECT data FROM user_state WHERE user_id=:u", {"u": uid})
    existing = (cur.fetchone() or [None])[0] if cur else None
    merged = {}
    if isinstance(existing, dict): merged.update(existing)
    if isinstance(data, dict):     merged.update(data)
    db_exec("""
        INSERT INTO user_state(user_id, intent, step, data, updated_at)
        VALUES (:u, COALESCE(:intent,'idle'), :step, CAST(:data AS jsonb), now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent = COALESCE(EXCLUDED.intent, user_state.intent),
            step   = EXCLUDED.step,
            data   = EXCLUDED.data,
            updated_at = now()
    """, {"u": uid, "intent": intent, "step": step, "data": json.dumps(merged)})

def get_state(uid: int) -> dict:
    if not engine: return {"intent":"idle","step":None,"data":{}}
    cur = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:u", {"u": uid})
    row = cur.fetchone() if cur else None
    if not row: return {"intent":"idle","step":None,"data":{}}
    return {"intent": row[0], "step": row[1], "data": row[2] or {}}

def save_error_row(uid: int, payload: dict):
    if not engine: return
    fields = {
        "user_id": uid,
        "error_text": payload.get("error_text","").strip(),
        "pattern_behavior": payload.get("pattern_behavior"),
        "pattern_emotion":  payload.get("pattern_emotion"),
        "pattern_thought":  payload.get("pattern_thought"),
        "positive_goal":    payload.get("positive_goal"),
        "tote_goal":        payload.get("tote_goal"),
        "tote_ops":         payload.get("tote_ops"),
        "tote_check":       payload.get("tote_check"),
        "tote_exit":        payload.get("tote_exit"),
        "checklist_pre":    payload.get("checklist_pre"),
        "checklist_post":   payload.get("checklist_post"),
    }
    db_exec("""
        INSERT INTO errors(user_id,error_text,pattern_behavior,pattern_emotion,pattern_thought,
                           positive_goal,tote_goal,tote_ops,tote_check,tote_exit,checklist_pre,checklist_post)
        VALUES (:user_id,:error_text,:pattern_behavior,:pattern_emotion,:pattern_thought,
                :positive_goal,:tote_goal,:tote_ops,:tote_check,:tote_exit,:checklist_pre,:checklist_post)
    """, fields)

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# Вспомогательные «мягкие» фразы
HELLO_VARIANTS = ("привет", "hi", "hello", "здрав", "дарова", "салют")
def is_hello(txt: str) -> bool:
    t = (txt or "").strip().lower()
    return any(t.startswith(w) for w in HELLO_VARIANTS)

def looks_vague(s: str) -> bool:
    # «иногда», «определённые дни», «бывает», «как получится» — просим конкретику
    vague = r"(иногда|порой|бывает|определённ(ые|ых)\sдн(и|я)|как\sполучится|когда\sкак|часто\sбывает)"
    return bool(re.search(vague, (s or "").lower()))

def ensure_behavior_level(s: str) -> bool:
    # done-условие для шага 1 (формулировка ошибки): должно быть действие/поведение
    # простая эвристика — наличие глаголов в инфинитиве/1л ед.ч. типичных для наших примеров
    verbs = r"(вхож(у|ить)|закрыва(ю|ть)|двига(ю|ть)|переза(хож(у|ить))|пропуска(ю|ть)|жду|спешу|скач(у|ить))"
    return bool(re.search(verbs, (s or "").lower()))

# ---------- GPT fallback ----------
def gpt_reply(user_text: str, context: str) -> Optional[str]:
    if not client: return None
    try:
        prompt = (
            "Ты — коуч-наставник трейдера. Отвечай кратко, тепло, по-деловому.\n"
            "Если пользователь ушёл в сторону от сценария, мягко ответь по сути, "
            "затем верни к следующему шагу сценария одной фразой.\n\n"
            f"Контекст шага:\n{context}\n\n"
            f"Сообщение ученика: {user_text}\n"
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":"Ты помогатор по Innertrade."},
                      {"role":"user","content": prompt}],
            temperature=0.3,
            max_tokens=220
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"OpenAI error: {e}")
        return None

# ---------- Команды ----------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    ensure_user(m.from_user.id)
    save_state(m.from_user.id, intent="idle", step=None, data={})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*.\n"
        "Можем поговорить свободно или пойти по шагам. Чем займёмся?\n"
        "_Команды: /status /ping_",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    # Короткая самодиагностика
    db_ok = False
    try:
        if engine:
            db_exec("SELECT 1")
            db_ok = True
    except Exception:
        db_ok = False
    gpt_ok = bool(client) and ALLOW_GPT == "1"
    bot.send_message(
        m.chat.id,
        f"Статус: ✅ бот живой\nБД: {'✅' if db_ok else '⚠️ off'}\nGPT: {'✅' if gpt_ok else '—'}\nРежим: {MODE}",
        reply_markup=main_menu()
    )

# ---------- Интенты (кнопки) ----------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error_btn(m): return intent_error(m)

def intent_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="error", step="ask_error", data={"error_payload":{}})
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку **1–2 предложениями** *на уровне поведения/навыка*.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy_btn(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="strategy", step=None)
    bot.send_message(
        m.chat.id,
        "Ок, соберём ТС по конструктору:\n"
        "1) Цели · 2) Стиль · 3) Рынки/инструменты\n"
        "4) Вход/выход · 5) Риск · 6) Сопровождение · 7) Тестирование",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport_btn(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="passport", step=None)
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel_btn(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="week_panel", step=None)
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic_btn(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="panic", step=None)
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что выбило)\n5) Вернись к плану или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help_btn(m):
    uid = m.from_user.id
    ensure_user(uid)
    save_state(uid, intent="start_help", step=None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

# ---------- Диалог по MERCEDES (урок 1) ----------
def ask_next_mercedes(m, st: dict):
    step = st.get("step")
    data = st.get("data", {})
    payload = data.get("error_payload", {})

    order = [
        ("ask_context",  "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)"),
        ("ask_emotions", "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)"),
        ("ask_thoughts", "МЫСЛИ. Что говоришь себе в этот момент? (цитатами, 1–2 фразы)"),
        ("ask_behavior", "ПОВЕДЕНИЕ. Что ты конкретно делаешь? Опиши действие глаголами (1–2 предложения)"),
        ("ask_goal",     "Сформулируй *позитивную цель* будущего поведения (что будешь делать по плану). 1 предложение."),
        ("ask_tote",     "Теперь TOTE.\n*T (цель)*: как звучит цель в терминах будущего поведения на 3 сделки подряд?"),
    ]
    next_key = None
    if step == "ask_error": next_key = "ask_context"
    else:
        for i, (key, _) in enumerate(order):
            if step == key and i+1 < len(order):
                next_key = order[i+1][0]
                break
    if not next_key:
        # закончить цикл TOTE уточнениями
        save_state(m.from_user.id, intent="error", step="ask_tote_ops")
        bot.send_message(m.chat.id, "O (операции): какие шаги предпримешь, чтобы удержать цель? (чек-лист из 2–4 пунктов)")
        return

    # спросить следующий блок
    save_state(m.from_user.id, intent="error", step=next_key)
    text_map = dict(order)
    bot.send_message(m.chat.id, text_map[next_key])

@bot.message_handler(func=lambda msg: get_state(msg.from_user.id).get("intent")=="error" and get_state(msg.from_user.id).get("step") is not None, content_types=["text"])
def error_flow(m):
    uid = m.from_user.id
    st = get_state(uid)
    step = st.get("step")
    data = st.get("data", {})
    payload = data.get("error_payload", {}) or {}

    user_text = (m.text or "").strip()

    # Спец-мягкость: привет/уточнения
    if is_hello(user_text):
        bot.send_message(m.chat.id, "Привет! Бережно двигаемся по шагам. Если что — можно уточнять по пути. 🙂")
        return

    # Обработка шагов
    if step == "ask_error":
        # done-условие: конкретика на уровне поведения
        if not ensure_behavior_level(user_text) or looks_vague(user_text):
            bot.send_message(
                m.chat.id,
                "Хочу зафиксировать **конкретное действие**. Пример: «вхожу до формирования сигнала».\n"
                "Попробуй переформулировать на *уровне поведения/навыка* (1–2 предложения)."
            )
            return
        payload["error_text"] = user_text
        save_state(uid, intent="error", step="ask_context", data={"error_payload": payload})
        bot.send_message(m.chat.id, "Принято. Пойдём дальше.")
        bot.send_message(m.chat.id, "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)")
        return

    elif step == "ask_context":
        if looks_vague(user_text):
            bot.send_message(m.chat.id, "Звучит общо. Можно точнее: «после долгого флэта», «после серии стопов», «когда хочу догнать план»?")
            return
        payload["pattern_context"] = user_text
        save_state(uid, intent="error", step="ask_emotions", data={"error_payload": payload})
        bot.send_message(m.chat.id, "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)")
        return

    elif step == "ask_emotions":
        payload["pattern_emotion"] = user_text
        save_state(uid, intent="error", step="ask_thoughts", data={"error_payload": payload})
        bot.send_message(m.chat.id, "МЫСЛИ. Что говоришь себе в этот момент? (цитатами, 1–2 фразы)")
        return

    elif step == "ask_thoughts":
        payload["pattern_thought"] = user_text
        save_state(uid, intent="error", step="ask_behavior", data={"error_payload": payload})
        bot.send_message(m.chat.id, "ПОВЕДЕНИЕ. Что ты конкретно делаешь? Опиши действие глаголами (1–2 предложения)")
        return

    elif step == "ask_behavior":
        if not ensure_behavior_level(user_text):
            bot.send_message(m.chat.id, "Опиши именно действие (глаголами): например, «выхожу по первой коррекции», «двигаю стоп».")
            return
        payload["pattern_behavior"] = user_text
        save_state(uid, intent="error", step="ask_goal", data={"error_payload": payload})
        bot.send_message(m.chat.id, "Сформулируй *позитивную цель* будущего поведения (что будешь делать по плану). 1 предложение.")
        return

    elif step == "ask_goal":
        payload["positive_goal"] = user_text
        save_state(uid, intent="error", step="ask_tote", data={"error_payload": payload})
        bot.send_message(m.chat.id, "Теперь TOTE.\n*T (цель)*: как звучит цель в терминах будущего поведения на 3 сделки подряд?")
        return

    elif step == "ask_tote":
        payload["tote_goal"] = user_text
        save_state(uid, intent="error", step="ask_tote_ops", data={"error_payload": payload})
        bot.send_message(m.chat.id, "O (операции): какие шаги предпримешь, чтобы удержать цель? (чек-лист из 2–4 пунктов)")
        return

    elif step == "ask_tote_ops":
        payload["tote_ops"] = user_text
        save_state(uid, intent="error", step="ask_tote_check", data={"error_payload": payload})
        bot.send_message(m.chat.id, "T (проверка): как поймёшь, что идёшь по плану? Критерий на сделку/серию.")
        return

    elif step == "ask_tote_check":
        payload["tote_check"] = user_text
        save_state(uid, intent="error", step="ask_tote_exit", data={"error_payload": payload})
        bot.send_message(m.chat.id, "E (выход): что считаем завершением цикла? (например: *3 сделки подряд без сдвига стопа*)")
        return

    elif step == "ask_tote_exit":
        payload["tote_exit"] = user_text

        # Чек-листы (минимум стандарт)
        payload["checklist_pre"]  = "• Чек-лист входа выполнен\n• Сетап 100%\n• Я в ресурсе (нет спешки/тревоги)"
        payload["checklist_post"] = "• Не трогал стоп/тейк\n• Вышел по сценарию\n• Короткая заметка в бланк"

        # Сохранить карточку ошибки
        save_error_row(uid, payload)

        # Сброс состояния
        save_state(uid, intent="idle", step=None, data={"error_payload":{}})

        # Итог
        bot.send_message(
            m.chat.id,
            "Готово. Зафиксировал разбор:\n"
            "• Паттерн: поведение/эмоции/мысли/контекст\n"
            "• Позитивная цель\n• TOTE (цель/операции/проверка/выход)\n"
            "• Чек-листы перед/после входа\n\n"
            "Хочешь добавить это в фокус недели?"
        )
        return

    # На всякий случай — страховка
    ask_next_mercedes(m, st)

# ---------- Свободный текст / болтовня / off-script ----------
@bot.message_handler(content_types=["text"])
def fallback(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    intent = st.get("intent") or "idle"
    step   = st.get("step")

    txt = (m.text or "").strip()

    # Дружелюбное «привет»
    if is_hello(txt):
        bot.send_message(
            m.chat.id,
            "Привет! Можем поговорить свободно или заняться задачей. Если хочешь — просто расскажи, что болит сейчас.",
            reply_markup=main_menu()
        )
        return

    # Если мы внутри сценария error — отдаём в error_flow (но сюда попадём, если step=None)
    if intent == "error" and step:
        return  # до сюда обычно не дойдём — есть отдельный хендлер

    # Off-script: короткая помощь + мягкий возврат к шагам
    context = f"intent={intent}, step={step}"
    reply = gpt_reply(txt, context) if client else None
    if reply:
        bot.send_message(m.chat.id, reply, reply_markup=main_menu())
    else:
        # без GPT — просто мягко
        bot.send_message(
            m.chat.id,
            "Понимаю. Давай сделаем так: можешь коротко описать, что именно случилось, "
            "или нажми кнопку ниже — начнём с разбора ошибки.",
            reply_markup=main_menu()
        )

# ---------- Flask (Webhook) ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat()+"Z"})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Проверка секрета
    if TG_WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    # Ограничение размера тела (≈1 МБ)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_json(force=True, silent=True)
    if not update: return "no update", 200
    bot.process_new_updates([types.Update.de_json(update)])
    return "ok", 200

def start_polling():
    try:
        bot.remove_webhook()
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")
    logging.info("Starting polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)

if __name__ == "__main__":
    if MODE == "polling":
        t = threading.Thread(target=start_polling, daemon=True)
        t.start()
        port = int(os.getenv("PORT", "10000"))
        app.run(host="0.0.0.0", port=port)
    else:
        # webhook-режим — просто поднимаем Flask
        port = int(os.getenv("PORT", "10000"))
        logging.info("Flask up (webhook mode)")
        app.run(host="0.0.0.0", port=port)

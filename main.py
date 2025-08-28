# main.py
import os
import json
import logging
from datetime import datetime

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from openai import OpenAI

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("innertrade")

# ----------------- ENV -----------------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH")           # например: wbhk_9t3x
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")      # произвольный секрет

for key, val in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "PUBLIC_URL": PUBLIC_URL,
    "WEBHOOK_PATH": WEBHOOK_PATH,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET
}.items():
    if not val:
        raise RuntimeError(f"{key} missing")

# ----------------- OPENAI -----------------
client = OpenAI(api_key=OPENAI_API_KEY)

def gpt_reply(system_prompt: str, user_prompt: str) -> str:
    """Короткий, тёплый ответ, 1 уточняющий вопрос, без «лекций»."""
    try:
        rsp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],
            max_output_tokens=220,
            temperature=0.3
        )
        return (rsp.output_text or "").strip()
    except Exception as e:
        log.warning(f"OpenAI error: {e}")
        return "Слышал тебя. Можем обсудить это подробнее или пойти по шагам."

# ----------------- DB -----------------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
        with engine.begin() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id      BIGINT PRIMARY KEY,
                intent       TEXT,
                step         TEXT,
                data         JSONB,
                updated_at   TIMESTAMPTZ DEFAULT now()
            );
            """))
        log.info("DB connected & user_state ready")
    except OperationalError as e:
        log.warning(f"DB not available: {e}")
        engine = None
else:
    log.info("DATABASE_URL not set — running without DB")

def get_state(uid: int) -> dict:
    st = {"intent": "greet", "step": None, "data": {}}
    if not engine:
        return st
    with engine.begin() as conn:
        row = conn.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:uid"),
                           {"uid": uid}).mappings().first()
        if not row:
            conn.execute(text(
                "INSERT INTO user_state(user_id, intent, step, data) VALUES (:uid, :i, :s, '{}'::jsonb)"),
                {"uid": uid, "i": st["intent"], "s": st["step"]})
            return st
        st["intent"] = row["intent"]
        st["step"]   = row["step"]
        st["data"]   = row["data"] or {}
        return st

def save_state(uid: int, intent: str | None = None, step: str | None = None, data_patch: dict | None = None):
    if not engine:
        return
    st = get_state(uid)
    if intent is not None:
        st["intent"] = intent
    if step is not None or step is None:
        st["step"] = step
    if data_patch:
        # лёгкая мердж-логика
        base = st.get("data") or {}
        base.update(data_patch)
        st["data"] = base
    with engine.begin() as conn:
        conn.execute(text("""
        INSERT INTO user_state (user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, COALESCE(:data,'{}'::jsonb), now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent,
            step=EXCLUDED.step,
            data=EXCLUDED.data,
            updated_at=now()
        """), {"uid": uid, "intent": st["intent"], "step": st["step"], "data": json.dumps(st["data"])})

# ----------------- TELEGRAM -----------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

def address_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("ты", "вы")
    return kb

BOT_NAME = "Kai"

def coach_system_prompt(address: str) -> str:
    # адрес: "ты" | "вы"
    polite = "на ты" if address == "ты" else "на вы"
    return (
        f"Ты тёплый, спокойный коуч по трейдингу, общаешься {polite}. "
        "Цель — мягко выслушать, задать 1 уточняющий вопрос и при необходимости вернуть к шагам. "
        "Не читай лекции. Коротко, по делу, максимум 2–3 предложения. "
        "Если собеседник спросил «как тебя зовут», ответь: «Я Kai». "
        "Если человек просит помощи с ошибкой — уточни пример и триггер, "
        "но не дави и не спеши к структуре. Никаких названий методик, пока не спросят."
    )

# ----------------- ДИАЛОГОВАЯ ЛОГИКА -----------------

def ensure_address(uid: int, chat_id: int) -> str:
    st = get_state(uid)
    data = st.get("data", {})
    address = data.get("address", None)
    if not address:
        # предложим, но не блокируем диалог
        bot.send_message(chat_id, "Как удобнее общаться — *ты* или *вы*? (можешь выбрать ниже)", reply_markup=address_kb())
        save_state(uid, data_patch={"address": "ты"})  # дефолт — «ты»
        address = "ты"
    return address

def increment_counter(uid: int, key: str) -> int:
    st = get_state(uid)
    data = st.get("data", {})
    val = int(data.get(key, 0)) + 1
    save_state(uid, data_patch={key: val})
    return val

def summarize_problem_text(txt: str) -> str:
    # короткое «обобщение» без дословного копипаста
    return f"Суммарно вижу: *есть трудность со следованием своим правилам* — вмешиваешься в сделку/сдвигаешь стоп/фиксируешь рано."

# ----------------- ХЕНДЛЕРЫ -----------------

@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    uid = m.from_user.id
    save_state(uid, intent="greet", step=None, data_patch={"clarify_count": 0})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*. "
        "Можем спокойно поговорить — просто напиши, что болит в торговле. "
        "Или выбери пункт ниже.",
        reply_markup=main_menu()
    )
    ensure_address(uid, m.chat.id)

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    uid = m.from_user.id
    st = get_state(uid)
    db = "ok" if engine else "no-db"
    payload = {
        "ok": True,
        "time": datetime.utcnow().isoformat(timespec="seconds"),
        "intent": st.get("intent"),
        "step": st.get("step"),
        "db": db
    }
    bot.send_message(m.chat.id, f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```", parse_mode="Markdown")

# --- КНОПКИ МЕНЮ ---

@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    uid = m.from_user.id
    save_state(uid, intent="error", step="ask_problem", data_patch={"clarify_count": 0})
    bot.send_message(
        m.chat.id,
        "Окей. Расскажи коротко про основную ошибку (1–2 предложения, как это происходит на практике).",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    uid = m.from_user.id
    save_state(uid, intent="strategy", step=None)
    bot.send_message(
        m.chat.id,
        "Соберём ТС в два шага: 1) подход/ТФ/вход; 2) стоп/сопровождение/выход/риск. "
        "Готов? Напиши, каким рынком и таймфреймом занимаешься сейчас."
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    uid = m.from_user.id
    save_state(uid, intent="passport", step=None)
    bot.send_message(m.chat.id, "Паспорт трейдера — начнём с рынка/инструментов. Чем торгуешь сейчас?")

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel(m):
    uid = m.from_user.id
    save_state(uid, intent="week_panel", step=None)
    bot.send_message(m.chat.id, "Панель недели: какой фокус возьмём на ближайшие 7 дней? (1 узел)")

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    uid = m.from_user.id
    save_state(uid, intent="panic", step=None)
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что именно выбило)\n5) Вернись к плану сделки или закрой позицию по правилу"
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    uid = m.from_user.id
    save_state(uid, intent="start_help", step=None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так: 1) Заполним паспорт (1–2 мин) 2) Выберем фокус недели 3) Соберём скелет ТС.\n"
        "С чего начнём?"
    )

# --- УТОЧНЕНИЕ «ты/вы» ---
@bot.message_handler(func=lambda msg: msg.text in ["ты","вы"])
def set_address(m):
    uid = m.from_user.id
    save_state(uid, data_patch={"address": m.text.lower()})
    bot.send_message(m.chat.id, "Принято. Можем просто поговорить — расскажи, что сейчас болит, или выбери пункт ниже.", reply_markup=main_menu())

# --- СВОБОДНЫЙ ТЕКСТ И СЦЕНАРИИ ---

def handle_error_flow(uid: int, chat_id: int, text_in: str):
    st = get_state(uid)
    data = st.get("data", {})
    step = st.get("step")

    if step == "ask_problem":
        # 2 шага мягкой конкретизации перед структурой
        cc = increment_counter(uid, "clarify_count")
        address = data.get("address", "ты")
        if cc <= 2:
            # мягкое уточнение через GPT
            sys = coach_system_prompt(address)
            u = f"Человек описывает ошибку так:\n{text_in}\nПопроси уточнить конкретику примера и триггера. Одно короткое уточнение."
            reply = gpt_reply(sys, u)
            # параллельно приготовим нашу «свертку»
            save_state(uid, data_patch={"last_problem_raw": text_in})
            bot.send_message(chat_id, reply)
            return
        else:
            summary = summarize_problem_text(text_in)
            save_state(uid, step="confirm_problem", data_patch={"problem_summary": summary})
            bot.send_message(chat_id, f"{summary}\n\nТак подходит? Если да — напиши *да*. Если нет — напиши, что добавить/исправить.")
            return

    if step == "confirm_problem":
        yes = text_in.strip().lower()
        if yes in ("да","ага","ок","верно","подходит"):
            save_state(uid, step="mer_context")
            bot.send_message(chat_id, "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)")
            return
        else:
            # ещё одна попытка уточнить
            save_state(uid, step="ask_problem", data_patch={"clarify_count": 0})
            bot.send_message(chat_id, "Окей, давай уточним ещё раз: опиши, пожалуйста, как именно это проявляется в сделке (коротко).")
            return

    # MERCEDES чётко по шагам
    if step == "mer_context":
        save_state(uid, step="mer_emotions", data_patch={"mer_context": text_in})
        bot.send_message(chat_id, "ЭМОЦИИ. Что чувствуешь в момент ошибки? (несколько слов)")
        return

    if step == "mer_emotions":
        save_state(uid, step="mer_thoughts", data_patch={"mer_emotions": text_in})
        bot.send_message(chat_id, "МЫСЛИ. Что говоришь себе в этот момент? (1–2 фразы)")
        return

    if step == "mer_thoughts":
        save_state(uid, step="mer_behavior", data_patch={"mer_thoughts": text_in})
        bot.send_message(chat_id, "ПОВЕДЕНИЕ. Что именно делаешь? Опиши действие глаголами (1–2 предложения).")
        return

    if step == "mer_behavior":
        save_state(uid, step="new_goal", data_patch={"mer_behavior": text_in})
        bot.send_message(chat_id, "Сформулируй новую цель одним предложением — что хочешь делать вместо прежнего поведения?")
        return

    if step == "new_goal":
        # переходим к TOTE, но без названий методик
        save_state(uid, step="tote_ops", data_patch={"new_goal": text_in})
        bot.send_message(chat_id, "Ок. Какие 2–3 шага помогут держаться этой цели в ближайших 3 сделках?")
        return

    if step == "tote_ops":
        save_state(uid, step=None, intent="idle", data_patch={"tote_ops": text_in})
        bot.send_message(chat_id, "Принято. Сохранили. Готов продолжать — скажи слово, или выбери пункт меню.", reply_markup=main_menu())
        return

    # если почему-то шаг не распознан, откатимся в мягкий диалог
    save_state(uid, intent="greet", step=None)
    bot.send_message(chat_id, "Окей. Можем продолжить разговор свободно или выбрать пункт в меню.", reply_markup=main_menu())

@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()
    st = get_state(uid)
    intent = st.get("intent", "greet")
    step   = st.get("step")
    data   = st.get("data", {}) or {}
    address = ensure_address(uid, m.chat.id)

    low = txt.lower()
    # Простой FAQ: «как тебя зовут»
    if "как тебя зовут" in low or "тебя зовут" in low:
        bot.send_message(m.chat.id, f"Я {BOT_NAME} 🙂")
        return

    # Если в режиме «ошибка» — ведём сценарий
    if intent == "error":
        handle_error_flow(uid, m.chat.id, txt)
        return

    # Иначе — мягкий «коуч-режим» через OpenAI
    sys = coach_system_prompt(address)
    reply = gpt_reply(sys, txt)

    # не прыгать сразу к структуре: 1–2 свободных обмена → потом предложить «разобрать по шагам»
    free_cnt = increment_counter(uid, "free_talk_count")
    if free_cnt >= 2 and any(k in low for k in ["ошиб", "просад", "правил", "стоп", "усредн"]):
        reply += "\n\nЕсли хочешь, можем аккуратно разобрать это по шагам. Напиши: *разбор ошибки*."
    bot.send_message(m.chat.id, reply)

# Короткий триггер на «разбор ошибки»
@bot.message_handler(func=lambda msg: msg.text and msg.text.strip().lower() in ["разбор ошибки","разобрать ошибку","мерседес","давай разбор"])
def start_error_from_free(m):
    return intent_error(m)

# ----------------- FLASK / WEBHOOK -----------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # проверка секрета
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_data().decode("utf-8")
    try:
        bot.process_new_updates([types.Update.de_json(update)])
    except Exception as e:
        log.error(f"process update error: {e}")
    return jsonify(ok=True)

@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "time": datetime.utcnow().isoformat(timespec="seconds")
    })

def setup_webhook():
    # удаляем и ставим новый
    try:
        bot.remove_webhook()
    except Exception:
        pass
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    ok = bot.set_webhook(
        url=url,
        secret_token=TG_WEBHOOK_SECRET,
        allowed_updates=["message","callback_query"],
        max_connections=40
    )
    log.info(f"Webhook set to {url}: {ok}")

if __name__ == "__main__":
    setup_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting web server…")
    app.run(host="0.0.0.0", port=port)

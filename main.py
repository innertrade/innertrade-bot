# main.py
import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from telebot.types import Update

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from openai import OpenAI

# =========================
# ЛОГИ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("innertrade")

# =========================
# ENV
# =========================
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
DATABASE_URL      = os.getenv("DATABASE_URL")
PUBLIC_URL        = os.getenv("PUBLIC_URL")
WEBHOOK_PATH      = os.getenv("WEBHOOK_PATH")           # напр. wbhk_9t3x
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")      # любой секрет для X-Telegram-Bot-Api-Secret-Token

for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "DATABASE_URL": DATABASE_URL,
    "PUBLIC_URL": PUBLIC_URL,
    "WEBHOOK_PATH": WEBHOOK_PATH,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET,
}.items():
    if not v:
        raise RuntimeError(f"Missing ENV: {k}")

# =========================
# КЛИЕНТЫ
# =========================
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")
ai  = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# DB
# =========================
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def db_ok() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception as e:
        log.error(f"DB check error: {e}")
        return False

def ensure_user(uid: int):
    try:
        with engine.begin() as c:
            c.execute(text("""
                INSERT INTO users(user_id) VALUES (:uid)
                ON CONFLICT (user_id) DO NOTHING
            """), {"uid": uid})
    except SQLAlchemyError as e:
        log.error(f"ensure_user: {e}")

def set_state(uid: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[dict]=None):
    try:
        with engine.begin() as c:
            c.execute(text("""
                INSERT INTO user_state(user_id, intent, step, data, updated_at)
                VALUES (:uid, :intent, :step, COALESCE(:data,'{}'::jsonb), NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    intent = COALESCE(:intent, user_state.intent),
                    step   = COALESCE(:step, user_state.step),
                    data   = COALESCE(:data, user_state.data),
                    updated_at = NOW()
            """), {"uid": uid, "intent": intent, "step": step, "data": json.dumps(data) if isinstance(data, dict) else data})
    except SQLAlchemyError as e:
        log.error(f"set_state: {e}")

def get_state(uid: int) -> Dict[str, Any]:
    try:
        with engine.begin() as c:
            r = c.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:uid"), {"uid": uid}).mappings().first()
            if r:
                return {"intent": r["intent"], "step": r["step"], "data": r["data"] or {}}
            return {"intent": "greet", "step": None, "data": {}}
    except SQLAlchemyError as e:
        log.error(f"get_state: {e}")
        return {"intent": "greet", "step": None, "data": {}}

def save_error_block(uid: int, fields: Dict[str, Optional[str]]):
    # fields: error_text, pattern_behavior, pattern_emotion, pattern_thought,
    #         positive_goal, tote_goal, tote_ops, tote_check, tote_exit, checklist_pre, checklist_post
    try:
        with engine.begin() as c:
            cols = ["user_id"] + list(fields.keys())
            vals = {**fields, "user_id": uid}
            sql_cols = ", ".join(cols)
            sql_params = ", ".join([f":{k}" for k in cols])
            c.execute(text(f"INSERT INTO errors ({sql_cols}) VALUES ({sql_params})"), vals)
    except SQLAlchemyError as e:
        log.error(f"save_error_block: {e}")

# =========================
# ВСПОМОГАТЬ
# =========================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Собрать ТС")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

def paraphrase_error(text_ru: str) -> str:
    # Небольшая переформулировка с GPT — без «допроса» и без термина «поведение/навык»
    try:
        msg = [
            {"role":"system","content":"Коротко перефразируй трейдерскую проблему простыми словами (1 предложение). Без оценок и советов."},
            {"role":"user","content": text_ru}
        ]
        rsp = ai.chat.completions.create(model="gpt-4o-mini", messages=msg, temperature=0.2, max_tokens=60)
        return rsp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"paraphrase_error fallback: {e}")
        return text_ru.strip()

def gentle_probe(previous: str) -> str:
    # 2–3 мягких уточнения перед MERCEDES
    try:
        msg = [
            {"role":"system","content":"Ты доброжелательный коуч по трейдингу. Задай один короткий уточняющий вопрос по проблеме, чтобы сделать её конкретнее (уровень действия), без оценок и терминов. 1 вопрос."},
            {"role":"user","content": previous}
        ]
        rsp = ai.chat.completions.create(model="gpt-4o-mini", messages=msg, temperature=0.2, max_tokens=80)
        return rsp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"gentle_probe fallback: {e}")
        return "Правильно ли я понял суть? Где именно это чаще всего случается — в момент входа, сопровождения или выхода?"

def short_coach_reply(user_text: str, context: Dict[str,Any]) -> str:
    # Свободный разговор «на первой линии» — мягко, по делу, но без ухода в длинные лекции
    try:
        sys = (
            "Ты эмпатичный наставник Innertrade. Общайся коротко и по делу, простым языком. "
            "Если человек делится болью — отзеркаль, уточни 1 деталь и предложи мягкий следующий шаг. "
            "Не дави «структурой», не упоминай внутренние термины (MERCEDES/TOTE). "
            "Не давай финансовых советов. Не проси личные данные. 1–2 предложения."
        )
        msgs = [{"role":"system","content":sys},{"role":"user","content":user_text}]
        rsp = ai.chat.completions.create(model="gpt-4o-mini", messages=msgs, temperature=0.4, max_tokens=90)
        return rsp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"short_coach_reply fallback: {e}")
        return "Понимаю. Расскажи, на каком этапе сделки это чаще всего всплывает — вход, сопровождение или выход?"

def want_move_to_mercedes(track: Dict[str,Any]) -> bool:
    # Как только есть короткая формулировка + 1–2 уточнения — можно переходить
    probes = track.get("probes_count", 0)
    err    = (track.get("error_text") or "").strip()
    return len(err) > 0 and probes >= 2

# =========================
# FLASK (ВЕБХУК + HEALTH/STATUS)
# =========================
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat()})

@app.get("/status")
def status():
    ok = db_ok()
    # пробуем вытащить одного (для простоты, без auth)
    sample = None
    try:
        with engine.connect() as c:
            r = c.execute(text("SELECT user_id,intent,step FROM user_state ORDER BY updated_at DESC LIMIT 1")).mappings().first()
            if r:
                sample = dict(r)
    except Exception:
        sample = None
    return jsonify({
        "ok": True,
        "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "intent": sample["intent"] if sample else None,
        "step": sample["step"] if sample else None,
        "db": "ok" if ok else "fail"
    })

MAX_BODY = 1_000_000

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Секрет-хедер Telegram: X-Telegram-Bot-Api-Secret-Token
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    cl = request.content_length or 0
    if cl > MAX_BODY:
        abort(413)
    try:
        update = Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        log.error(f"webhook error: {e}")
        return "ERR", 500

# =========================
# ХЕНДЛЕРЫ
# =========================

@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="greet", step=None, data={"probes_count":0,"error_text":""})
    name = (m.from_user.first_name or "").strip() or "друг"
    bot.send_message(
        m.chat.id,
        f"👋 Привет, {name}! Можем просто поговорить — что болит в торговле — или выбрать пункт ниже.",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    uid = m.from_user.id
    st = get_state(uid)
    ok = db_ok()
    bot.send_message(
        m.chat.id,
        "```\n" + json.dumps({
            "ok": True,
            "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "intent": st.get("intent"),
            "step": st.get("step"),
            "db": "ok" if ok else "fail",
        }, ensure_ascii=False, indent=2) + "\n```",
        parse_mode="Markdown"
    )

# ----- Кнопки меню -----

@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="error", step="ask_error", data={"probes_count":0,"error_text":""})
    bot.send_message(
        m.chat.id,
        "Расскажи коротко, что именно идёт не так. Не переживай о формулировках — просто как есть.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Собрать ТС")
def btn_ts(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="ts", step="intro")
    bot.send_message(
        m.chat.id,
        "Начнём со стиля и входа. Как обычно ты заходишь в сделку и на каких ТФ? (позже дополним стоп/сопровождение/лимиты)",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="passport", step="intro")
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. Давай начнём с рынков/инструментов и таймфреймов, на которых планируешь работать.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="week_panel", step="focus")
    bot.send_message(
        m.chat.id,
        "Панель недели: какой один фокус возьмём на ближайшие 7 дней?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def btn_panic(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="panic", step="ritual")
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 минуты\n2) Закрой график на 5 минут\n3) 10 медленных вдохов\n4) Запиши триггер\n5) Вернись к плану сделки или закрой по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def btn_start_help(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="start_help", step="choose")
    bot.send_message(
        m.chat.id,
        "Предлагаю так: 1) быстро зафиксируем текущую боль, 2) определим фокус недели, 3) соберём каркас ТС. С чего начнём?",
        reply_markup=main_menu()
    )

# ----- Ядро сценария «Ошибка» -----

def handle_error_flow(m, st):
    uid = m.from_user.id
    data = st.get("data") or {}
    step = st.get("step")

    txt = (m.text or "").strip()

    # Шаг 1: запросить проблему (и 2–3 мягких уточнения до MERCEDES)
    if step in (None, "ask_error"):
        if not data.get("error_text"):
            # первая формулировка
            data["error_text"] = txt
            data["probes_count"] = 0
            set_state(uid, intent="error", step="probe", data=data)
            probe = gentle_probe(txt)
            bot.send_message(m.chat.id, probe)
            return
        else:
            # уже была, уточняем дальше
            data["probes_count"] = int(data.get("probes_count", 0)) + 1
            # аккумулируем контекст
            data["error_text"] = (data["error_text"] + " | " + txt).strip()
            if want_move_to_mercedes(data):
                # перефраз — НЕ дословно
                p = paraphrase_error(data["error_text"])
                data["error_paraphrase"] = p
                set_state(uid, intent="error", step="confirm", data=data)
                bot.send_message(
                    m.chat.id,
                    f"Суммирую так: *{p}*\nПодходит? Если хочется — добавь/исправь одним предложением."
                )
                return
            else:
                set_state(uid, intent="error", step="probe", data=data)
                probe = gentle_probe(data["error_text"])
                bot.send_message(m.chat.id, probe)
                return

    if step == "probe":
        data["probes_count"] = int(data.get("probes_count", 0)) + 1
        data["error_text"] = (data.get("error_text","") + " | " + txt).strip()
        if want_move_to_mercedes(data):
            p = paraphrase_error(data["error_text"])
            data["error_paraphrase"] = p
            set_state(uid, intent="error", step="confirm", data=data)
            bot.send_message(
                m.chat.id,
                f"Суммирую так: *{p}*\nПодходит? Если хочется — добавь/исправь одним предложением."
            )
            return
        else:
            set_state(uid, intent="error", step="probe", data=data)
            probe = gentle_probe(data["error_text"])
            bot.send_message(m.chat.id, probe)
            return

    # Подтверждение
    if step == "confirm":
        # любое короткое «да/ок/норм» — двигаемся дальше; иначе — принимаем уточнение и ещё раз подтверждаем
        ack = txt.lower()
        if any(w in ack for w in ["да","ок","ага","подходит","норм","верно","согласен","согласна","супер"]):
            set_state(uid, intent="error", step="mer_context", data=data)
            bot.send_message(m.chat.id, "Окей. КОНТЕКСТ — когда это обычно всплывает? Что предшествует?")
            return
        else:
            # приняли правку, ещё раз перефразируем коротко
            merged = (data.get("error_paraphrase","") + " | " + txt).strip()
            p2 = paraphrase_error(merged)
            data["error_paraphrase"] = p2
            set_state(uid, intent="error", step="confirm", data=data)
            bot.send_message(m.chat.id, f"Исправил формулировку: *{p2}*\nПодходит?")
            return

    # MERCEDES (короткая версия: контекст → эмоции → мысли → поведение)
    if step == "mer_context":
        data["mer_context"] = txt
        set_state(uid, intent="error", step="mer_emotions", data=data)
        bot.send_message(m.chat.id, "ЭМОЦИИ — что чувствуешь в такие моменты? (несколько слов)")
        return

    if step == "mer_emotions":
        data["mer_emotions"] = txt
        set_state(uid, intent="error", step="mer_thoughts", data=data)
        bot.send_message(m.chat.id, "МЫСЛИ — что говоришь себе? (1–2 короткие фразы)")
        return

    if step == "mer_thoughts":
        data["mer_thoughts"] = txt
        set_state(uid, intent="error", step="mer_behavior", data=data)
        bot.send_message(m.chat.id, "ПОВЕДЕНИЕ — что делаешь конкретно? Опиши действие глаголами.")
        return

    if step == "mer_behavior":
        data["mer_behavior"] = txt
        # Резюме
        summary = (
            f"Резюме паттерна:\n"
            f"• Контекст: {data.get('mer_context','—')}\n"
            f"• Эмоции: {data.get('mer_emotions','—')}\n"
            f"• Мысли: {data.get('mer_thoughts','—')}\n"
            f"• Поведение: {data.get('mer_behavior','—')}"
        )
        set_state(uid, intent="error", step="goal", data=data)
        bot.send_message(m.chat.id, summary)
        bot.send_message(m.chat.id, "Сформулируем *новую цель* одним предложением: что хочешь делать вместо прежнего поведения?")
        return

    # Цель и короткий TOTE
    if step == "goal":
        data["positive_goal"] = txt
        set_state(uid, intent="error", step="tote_ops", data=data)
        bot.send_message(m.chat.id, "Хорошо. Какие 2–3 *шага* помогут держаться этой цели в ближайших 3 сделках?")
        return

    if step == "tote_ops":
        data["tote_ops"] = txt
        set_state(uid, intent="error", step="tote_check", data=data)
        bot.send_message(m.chat.id, "Критерий проверки: по каким признакам поймёшь, что получилось? (кратко)")
        return

    if step == "tote_check":
        data["tote_check"] = txt
        # Сохраняем блок в errors
        save_error_block(uid, {
            "error_text": data.get("error_paraphrase") or data.get("error_text"),
            "pattern_behavior": data.get("mer_behavior"),
            "pattern_emotion": data.get("mer_emotions"),
            "pattern_thought": data.get("mer_thoughts"),
            "positive_goal": data.get("positive_goal"),
            "tote_goal": data.get("positive_goal"),  # в краткой версии цель = TOTE.goal
            "tote_ops": data.get("tote_ops"),
            "tote_check": data.get("tote_check"),
            "tote_exit": None,
            "checklist_pre": None,
            "checklist_post": None
        })
        set_state(uid, intent="idle", step=None, data={})
        bot.send_message(
            m.chat.id,
            "Готово. Зафиксировал цель и шаги. Хочешь добавить это в *фокус недели* или двинемся дальше?",
            reply_markup=main_menu()
        )
        return

    # На всякий — мягкий ответ
    bot.send_message(m.chat.id, "Принял. Продолжим. Коротко опиши, что именно идёт не так — и двинемся шаг за шагом.")

# ----- Свободный текст / роутинг -----

@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    intent = st.get("intent") or "greet"

    t = (m.text or "").strip()

    # Явные интенты
    if intent == "error" or t.lower().startswith(("ошибка","просадк","нарушаю","не по сетапу")):
        # Если человек сам зашёл текстом — инициализируем сценарий
        if intent != "error":
            set_state(uid, intent="error", step="ask_error", data={"probes_count":0,"error_text":""})
        handle_error_flow(m, get_state(uid))
        return

    # В свободном режиме — короткий коуч-ответ с GPT
    reply = short_coach_reply(t, st)
    bot.send_message(m.chat.id, reply, reply_markup=main_menu())

# =========================
# СТАРТ СЕРВЕРА + ВЕБХУК
# =========================
def setup_webhook():
    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning(f"remove_webhook: {e}")
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    ok = bot.set_webhook(
        url=url,
        secret_token=TG_WEBHOOK_SECRET,
        allowed_updates=["message","callback_query"],
        drop_pending_updates=False,
        max_connections=40
    )
    log.info(f"set_webhook({url}) -> {ok}")

if __name__ == "__main__":
    setup_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)

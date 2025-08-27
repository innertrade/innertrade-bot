import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from telebot.types import Update

from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ----------------- ЛОГИ -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ----------------- ENV ------------------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DATABASE_URL     = os.getenv("DATABASE_URL")
PUBLIC_URL       = os.getenv("PUBLIC_URL")         # https://innertrade-bot.onrender.com
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH")       # например: wbhk_9t3x
TG_WEBHOOK_SECRET= os.getenv("TG_WEBHOOK_SECRET")  # любой твой UUID

for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "DATABASE_URL": DATABASE_URL,
    "PUBLIC_URL": PUBLIC_URL,
    "WEBHOOK_PATH": WEBHOOK_PATH,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET,
}.items():
    if not v:
        raise RuntimeError(f"Missing env: {k}")

# ----------------- OPENAI ----------------
oai = OpenAI(api_key=OPENAI_API_KEY)

def ask_gpt(system_prompt: str, user_prompt: str, fallback: str) -> str:
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"OpenAI fallback: {e}")
        return fallback

# ----------------- DB --------------------
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def db_exec(sql: str, params: Optional[dict] = None, fetch: bool = False):
    try:
        with engine.begin() as conn:
            res = conn.execute(text(sql), params or {})
            if fetch:
                return [dict(r._mapping) for r in res]
    except SQLAlchemyError as e:
        logging.error(f"DB error: {e}")
    return None

def ensure_user(user_id: int):
    db_exec("""
        INSERT INTO users(user_id) VALUES (:uid)
        ON CONFLICT (user_id) DO NOTHING
    """, {"uid": user_id})

def get_state(user_id: int) -> Dict[str, Any]:
    row = db_exec("SELECT intent, step, data FROM user_state WHERE user_id=:uid", {"uid": user_id}, fetch=True)
    if row and len(row):
        return {
            "intent": row[0]["intent"],
            "step": row[0]["step"],
            "data": (row[0]["data"] or {}) if isinstance(row[0]["data"], dict) else {}
        }
    return {"intent": "idle", "step": None, "data": {}}

def save_state(user_id: int, intent: Optional[str]=None, step: Optional[str]=None, data: Optional[dict]=None):
    cur = get_state(user_id)
    if intent is None: intent = cur["intent"]
    if step   is None: step   = cur["step"]
    merged = cur["data"].copy()
    if data: merged.update(data)
    db_exec("""
        INSERT INTO user_state(user_id, intent, step, data, updated_at)
        VALUES (:uid, :intent, :step, CAST(:data AS jsonb), now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=EXCLUDED.intent, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()
    """, {"uid": user_id, "intent": intent, "step": step, "data": json.dumps(merged)})

def append_error_row(user_id: int, fields: Dict[str, Any]):
    # создаём запись ошибки/разбора (минимум текст ошибки)
    db_exec("""
        INSERT INTO errors(user_id, error_text, pattern_behavior, pattern_emotion, pattern_thought,
                           positive_goal, tote_goal, tote_ops, tote_check, tote_exit, checklist_pre, checklist_post, created_at)
        VALUES (:user_id, :error_text, :pattern_behavior, :pattern_emotion, :pattern_thought,
                :positive_goal, :tote_goal, :tote_ops, :tote_check, :tote_exit, :checklist_pre, :checklist_post, now())
    """, {
        "user_id": user_id,
        "error_text": fields.get("error_text"),
        "pattern_behavior": fields.get("pattern_behavior"),
        "pattern_emotion": fields.get("pattern_emotion"),
        "pattern_thought": fields.get("pattern_thought"),
        "positive_goal": fields.get("positive_goal"),
        "tote_goal": fields.get("tote_goal"),
        "tote_ops": fields.get("tote_ops"),
        "tote_check": fields.get("tote_check"),
        "tote_exit": fields.get("tote_exit"),
        "checklist_pre": fields.get("checklist_pre"),
        "checklist_post": fields.get("checklist_post"),
    })

# ----------------- TELEGRAM --------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Собрать ТС")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤝 Поговорить")
    return kb

def yes_no_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Да", callback_data="ok_yes"),
           types.InlineKeyboardButton("Нет", callback_data="ok_no"),
           types.InlineKeyboardButton("Дополнить", callback_data="ok_more"))
    return kb

def polite(address: str, t: str, v: str) -> str:
    return t if address == "ты" else v

# ----------------- ПЕРВОЕ КАСАНИЕ --------
def start_dialog(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    data = st["data"]
    # Если не знаем как обращаться — спросим
    if not data.get("address"):
        bot.send_message(uid,
            "Привет! Как обращаться — *на ты* или *на вы*? Напиши: `ты` или `вы`.",
            reply_markup=main_menu())
        save_state(uid, intent="greet", step="ask_address")
        return
    # Иначе — короткий мягкий вход
    name = data.get("name") or m.from_user.first_name or ""
    hello = f"Привет{', ' + name if name else ''}! Рад тебя видеть."
    tail  = polite(data["address"], 
                   "Можем просто немного поговорить, а потом возьмёмся за конкретику. Что болит сейчас?",
                   "Можем немного пообщаться, а затем перейти к конкретике. Что сейчас беспокоит?")
    bot.send_message(uid, f"{hello}\n{tail}", reply_markup=main_menu())
    # Перейдём в режим лёгкого свободного разговора
    save_state(uid, intent="free_chat", step="warmup", data={"warmup_turns": 0})

def try_summarize_problem(uid: int, text_in: str, address: str) -> str:
    sys = ("Ты эмпатичный коуч трейдеров. Задача: на основе реплики сформулировать одну короткую, "
           "конкретную формулировку проблемы *на уровне действия/привычки* (без морали и диагноза). "
           "Не пиши лишнего, 1 предложение.")
    fallback = text_in.strip()
    phr = ask_gpt(sys, text_in, fallback)
    # Уберём кавычки и смягчим
    phr = phr.strip('“”"').strip()
    lead = polite(address, "Правильно ли понимаю, что ключевая загвоздка сейчас такая", 
                             "Верно ли я понимаю, что ключевая сложность сейчас такова")
    return f"{lead}: *{phr}* ?"

def continue_mercedes(uid: int, address: str):
    # задаём разбор без упоминания названий техник
    st = get_state(uid)
    d  = st["data"]
    flags = d.get("flags", {})
    # если пользователь ранее сказал, что «не зависит от ситуации», контекст пропускаем
    ask_lines = []
    if not flags.get("no_context"):
        ask_lines.append(polite(address,
            "Начнём с окружения: в каких условиях это чаще случается? (рабочий день, после серии, время и т.п.)",
            "Начнём с окружения: в каких условиях это чаще происходит? (рабочий день, после серии, время и т.п.)"
        ))
    ask_lines.append(polite(address,
        "Какие чувства поднимаются в эти моменты? (несколько слов)",
        "Какие чувства поднимаются в эти моменты? (несколько слов)"))
    ask_lines.append(polite(address,
        "Какие мысли мелькают? Напиши 1–2 коротких фразы в кавычках.",
        "Какие мысли появляются? Напишите 1–2 коротких фразы в кавычках."))
    ask_text = "\n\n".join(ask_lines)
    bot.send_message(uid, ask_text, reply_markup=main_menu())
    save_state(uid, intent="error", step="drill_collect")

def maybe_move_to_drill(uid: int):
    st = get_state(uid)
    d  = st["data"]
    # done-условие: есть behavior_line — короткая формулировка действия
    if d.get("behavior_line"):
        address = d.get("address", "ты")
        bot.send_message(uid,
            polite(address,
                   "Ок, у меня есть суть. Давай разберём ситуацию по полочкам и затем соберём план действий.",
                   "Хорошо, суть понятна. Давайте разберём ситуацию по шагам и затем соберём план действий."),
            reply_markup=main_menu())
        continue_mercedes(uid, address)
        return True
    return False

# ----------------- ХЕНДЛЕРЫ --------------
@bot.message_handler(commands=["start", "menu", "reset"])
def cmd_start(m):
    # Сбросим только сессию (БД-данные пользователя не трогаем)
    save_state(m.from_user.id, intent="idle", step=None, data={
        "address": None, "name": None, "warmup_turns": 0,
        "behavior_line": None, "flags": {}
    })
    start_dialog(m)

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = get_state(m.from_user.id)
    healthy = True
    try:
        db_exec("SELECT 1")
    except Exception:
        healthy = False
    info = {
        "ok": True,
        "time": datetime.utcnow().isoformat(),
        "intent": st["intent"],
        "step": st["step"],
        "db": "ok" if healthy else "err",
    }
    bot.send_message(m.chat.id, "```json\n" + json.dumps(info, ensure_ascii=False, indent=2) + "\n```")

# Кнопки меню (интенты)
@bot.message_handler(func=lambda msg: msg.text in ["🚑 У меня ошибка"])
def btn_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    data = st["data"]
    if not data.get("address"):
        start_dialog(m); return
    bot.send_message(uid, polite(data["address"],
        "Опиши проблему в паре предложений, как она проявляется в действии.",
        "Опишите проблему в паре предложений, как она проявляется в действии."), reply_markup=main_menu())
    save_state(uid, intent="error", step="collect_intro", data={"behavior_line": None})

@bot.message_handler(func=lambda msg: msg.text in ["🧩 Собрать ТС", "📄 Паспорт", "🗒 Панель недели", "🆘 Экстренно", "🤝 Поговорить"])
def btn_misc(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    addr = st["data"].get("address") or "ты"
    if m.text == "🤝 Поговорить":
        bot.send_message(uid, polite(addr,
            "Окей, просто расскажи, что сейчас волнует — я рядом.",
            "Хорошо, расскажите, что сейчас волнует — я рядом."), reply_markup=main_menu())
        save_state(uid, intent="free_chat", step="warmup", data={"warmup_turns": 0})
        return
    # Остальные пока короткими заглушками
    replies = {
        "🧩 Собрать ТС": "Соберём набросок системы позже — сейчас фокус на текущей задаче.",
        "📄 Паспорт": "Позже заполним Паспорт ключевыми данными.",
        "🗒 Панель недели": "В Панели недели зафиксируем фокус и ограничители.",
        "🆘 Экстренно": "Стоп. Сделай 10 медленных вдохов. Если позиция открыта и ты 'поплыл' — сократи объём или закрой по правилу. Потом напиши, что именно произошло одной строкой.",
    }
    bot.send_message(uid, replies[m.text], reply_markup=main_menu())

# Инлайн подтверждение формулировки
@bot.callback_query_handler(func=lambda c: c.data in ["ok_yes", "ok_no", "ok_more"])
def cb_confirm(c):
    uid = c.from_user.id
    st  = get_state(uid)
    d   = st["data"]
    addr= d.get("address","ты")
    if c.data == "ok_yes":
        # двигаемся к разбору
        bot.answer_callback_query(c.id, polite(addr, "Берём в работу", "Берём в работу"))
        maybe_move_to_drill(uid)
    elif c.data == "ok_no":
        bot.answer_callback_query(c.id)
        bot.send_message(uid, polite(addr,
            "Окей, напиши как бы ты сформулировал её сам — одной строкой.",
            "Хорошо, напишите, как бы вы сформулировали это сами — одной строкой."))
        save_state(uid, data={"behavior_line": None})
    else:
        bot.answer_callback_query(c.id)
        bot.send_message(uid, polite(addr,
            "Добавь, чего не хватает, и я обновлю формулировку.",
            "Добавьте, чего не хватает, и я уточню формулировку."))

# ----------------- ОБЩИЙ ТЕКСТ -----------
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    data = st["data"]
    text_in = (m.text or "").strip()
    # Адрес/имя настройка
    if st["intent"] == "greet" and st["step"] == "ask_address":
        lower = text_in.lower()
        if lower in ["ты", "вы"]:
            save_state(uid, data={"address": lower})
            bot.send_message(uid, polite(lower,
                "Как тебя зовут? (если удобно — просто имя/никнейм)",
                "Как вас зовут? (если удобно — просто имя/никнейм)"))
            save_state(uid, step="ask_name")
        else:
            bot.send_message(uid, "Напиши, пожалуйста, `ты` или `вы`.")
        return
    if st["intent"] == "greet" and st["step"] == "ask_name":
        save_state(uid, intent="free_chat", step="warmup", data={"name": text_in, "warmup_turns": 0})
        bot.send_message(uid, polite(data.get("address","ты"),
            "Рад знакомству! Расскажи коротко, что болит сейчас — и пойдём разбираться.",
            "Рад знакомству! Расскажите коротко, что беспокоит сейчас — и пойдём разбираться."),
            reply_markup=main_menu())
        return

    # Свободный разогрев: до 2-3 подходов — слушаем, перефразируем, мягко чиним формулировку
    if st["intent"] in ["idle", "free_chat"] or (st["intent"]=="error" and st["step"]=="collect_intro"):
        addr = data.get("address","ты")
        # Если человек прямо написал, что «не зависит от ситуации» — отметим флаг
        if "не зависит" in text_in.lower():
            flags = data.get("flags", {})
            flags["no_context"] = True
            save_state(uid, data={"flags": flags})

        # Попробуем сделать краткую «поведенческую» формулировку (без слов «поведение», «навык»)
        phr = try_summarize_problem(uid, text_in, addr)
        # Сохраним draft в data, но behavior_line подтвердим после «Да/Нет/Дополнить»
        save_state(uid, intent="error", step="confirm_problem", data={"draft_behavior_line": phr})
        bot.send_message(uid, phr, reply_markup=yes_no_kb())
        return

    # После подтверждения ученик может дополнять — ловим и обновляем
    if st["intent"] == "error" and st["step"] in ["confirm_problem", "collect_intro"]:
        addr = data.get("address","ты")
        # обновим перефраз
        joined = (data.get("draft_behavior_line","") + " " + text_in).strip()
        phr = try_summarize_problem(uid, joined, addr)
        save_state(uid, step="confirm_problem", data={"draft_behavior_line": phr})
        bot.send_message(uid, phr, reply_markup=yes_no_kb())
        return

    # Сбор деталей «разбора»
    if st["intent"] == "error" and st["step"] in ["drill_collect", "drill_more"]:
        addr = data.get("address","ты")
        # Накапливаем ответы
        bucket = data.get("drill", {})
        # простая эвристика распаковки
        low = text_in.lower()
        if any(k in low for k in ["злю", "страх", "тревог", "паник", "рад", "напряж"]):
            bucket["emotions"] = text_in
        if '"' in text_in or '«' in text_in or '»' in text_in or any(k in low for k in ["думаю", "кажется", "мысл"]):
            bucket["thoughts"] = text_in
        if any(k in low for k in ["день", "после", "утро", "вечер", "серия", "новости"]) and not data.get("flags",{}).get("no_context"):
            bucket["context"] = text_in
        save_state(uid, data={"drill": bucket})

        # Проверим, достаточно ли для сводки
        need = ["emotions", "thoughts"]
        if data.get("flags",{}).get("no_context"):
            have_all = all(k in bucket for k in need)
        else:
            have_all = all(k in bucket for k in (need + ["context"]))
        if have_all:
            # сформируем краткое резюме и позитивную цель
            raw = {
                "behavior": data.get("behavior_line") or (data.get("draft_behavior_line","").strip("* ?").split(":")[-1].strip()),
                "emotions": bucket.get("emotions"),
                "thoughts": bucket.get("thoughts"),
                "context":  bucket.get("context", "(не указано)"),
            }
            sys = ("Сжато переформулируй проблему трейдера как связку: действие → эмоции → мысли (1–2 предложения). "
                   "Затем предложи позитивную цель в формате: «Хочу научиться [конкретное действие]...». "
                   "Пиши по-русски, дружелюбно, без нотаций.")
            fallback = f"Суть: {raw['behavior']}. Эмоции: {raw['emotions']}. Мысли: {raw['thoughts']}."
            summ = ask_gpt(sys, json.dumps(raw, ensure_ascii=False), fallback)

            # сохраним и покажем
            save_state(uid, step="plan_goal", data={
                "behavior_line": raw["behavior"],
                "summary": summ
            })
            bot.send_message(uid, polite(addr,
                f"Резюме:\n{summ}\n\nСформулируй краткую цель одной строкой (что будешь делать иначе).",
                f"Резюме:\n{summ}\n\nСформулируйте краткую цель одной строкой (что будете делать иначе)."))
        else:
            # Просим ещё одну грань — максимум 2-3 вопроса
            asked = data.get("asked_drill", 0) + 1
            save_state(uid, data={"asked_drill": asked})
            if asked <= 3:
                bot.send_message(uid, polite(addr,
                    "Добавь ещё штрих (чувства/мысли/обстоятельства) — одно-две короткие фразы.",
                    "Добавьте ещё штрих (чувства/мысли/обстоятельства) — одну-две короткие фразы."))
            else:
                # Переходим дальше с тем, что есть
                continue_mercedes(uid, addr)
        return

    # Получаем формулировку цели → составляем короткий план (без упоминания названий техник)
    if st["intent"] == "error" and st["step"] == "plan_goal":
        addr = data.get("address","ты")
        goal = text_in.strip()
        # Сконструируем план «цель → действия → проверка → вывод»
        sys = ("На вход цель трейдера. Построй конкретный план из 4 блоков: "
               "Цель (1 строка), Шаги (3–5 маркеров), Проверка (что считаем выполнением), Вывод (что делать по итогу). "
               "Пиши кратко и конкретно, без названий техник.")
        plan = ask_gpt(sys, goal, f"Цель: {goal}\nШаги: 1) ... 2) ... 3) ...\nПроверка: ...\nВывод: ...")
        # Сохраним итог
        try:
            append_error_row(uid, {
                "error_text": data.get("behavior_line") or "",
                "pattern_behavior": data.get("behavior_line"),
                "pattern_emotion": data.get("drill",{}).get("emotions"),
                "pattern_thought": data.get("drill",{}).get("thoughts"),
                "positive_goal": goal,
                "tote_goal": goal,           # хранение в тех же полях, чтобы не плодить
                "tote_ops": plan,
                "tote_check": "",
                "tote_exit": "",
                "checklist_pre": "",
                "checklist_post": "",
            })
        except Exception as e:
            logging.warning(f"Could not persist error row: {e}")

        bot.send_message(uid, polite(addr,
            f"План готов:\n\n{plan}\n\nГотов двигаться дальше или что-то поправим?",
            f"План готов:\n\n{plan}\n\nГотовы двигаться дальше или что-то поправим?"), reply_markup=main_menu())
        save_state(uid, intent="idle", step=None)
        return

    # Иное: off-script → ответ GPT и мягкое возвращение
    addr = data.get("address","ты")
    sys = ("Ты эмпатичный наставник. Ответь по сути, коротко и по-доброму, затем предложи вернуться к текущей задаче.")
    answer = ask_gpt(sys, text_in, polite(addr, "Понимаю. Давай чуть сузим тему — опиши одним предложением, что именно делаешь в ошибочном моменте.", 
                                                 "Понимаю. Давайте чуть сузим тему — опишите одним предложением, что именно делаете в ошибочный момент."))
    bot.send_message(uid, answer, reply_markup=main_menu())

# ----------------- FLASK / WEBHOOK -------
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat()})

@app.get("/health")
def health():
    return jsonify({"status":"ok","time": datetime.utcnow().isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        upd = Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([upd])
    except Exception as e:
        logging.exception(f"Webhook error: {e}")
    return "OK"

# --------------- ЛОКАЛЬНЫЙ ПУСК ----------
if __name__ == "__main__":
    # Без автопостановки вебхука (ты ставишь вручную), просто стартуем Flask
    port = int(os.getenv("PORT", "10000"))
    logging.info("Starting app...")
    app.run(host="0.0.0.0", port=port)

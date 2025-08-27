# --- PATCH: мягкая префаза + подтверждение для интента "У меня ошибка"
from telebot import types
import re
import json
from datetime import datetime

FREE_ROUNDS = 3  # сколько реплик собрать до предложения формулировки
VAGUE_WORDS = ["определенн", "иногда", "бывает", "какие-то", "как-то", "периодически", "редко", "часто"]

BEHAVIOR_VERBS = [
    "вхожу","войти","закрываю","закрыть","двигаю","двигать","переношу",
    "усредняю","усреднить","пересиживаю","пересидеть","добавляю","добавить",
    "снижаю","снизить","повышаю","повысить","удваиваю","удвоить","фиксирую","зафиксировать",
    "не ставлю","ставлю","меняю","менять","прыгаю","прыгать","ломаю","ломать"
]

def _load_state(user_id: int):
    if not engine:
        return {"intent": None, "data": {}}
    with engine.connect() as conn:
        row = conn.execute(text("SELECT intent, COALESCE(data,'{}'::jsonb) FROM user_state WHERE user_id=:uid"),
                           {"uid": user_id}).fetchone()
        if not row:
            return {"intent": None, "data": {}}
        intent, data = row
        return {"intent": intent, "data": dict(data)}

def _merge_state(user_id: int, **patch):
    st = _load_state(user_id)
    data = st["data"] or {}
    data.update(patch)
    save_state(user_id, st.get("intent") or "idle", data)

def _push_note(user_id: int, note: str):
    st = _load_state(user_id)
    data = st["data"] or {}
    notes = data.get("free_notes", [])
    notes.append(note.strip())
    if len(notes) > 8:
        notes = notes[-8:]
    data["free_notes"] = notes
    save_state(user_id, st.get("intent") or "idle", data)

def _has_behavior(text_: str) -> bool:
    t = text_.lower()
    return any(v in t for v in BEHAVIOR_VERBS)

def _has_vague(text_: str) -> bool:
    t = text_.lower()
    return any(v in t for v in VAGUE_WORDS)

def _propose_summary(notes: list[str]) -> str:
    # Простая сборка: ищем контекст/эмоции/поведение по ключикам; при желании можно заменить на OpenAI.
    joined = " ".join(notes[-5:])
    # эвристики
    context = ""
    emotions = ""
    behavior = ""
    # контекст
    m_ctx = re.search(r"(когда|в дни|после|перед|в ситуац)[^\.]{5,80}", joined, flags=re.I)
    if m_ctx: context = m_ctx.group(0)
    # эмоции
    m_em = re.search(r"(тревог|страх|азарт|напряжен|паник|давлен)[^\.]{0,40}", joined, flags=re.I)
    if m_em: emotions = m_em.group(0)
    # поведение
    for v in BEHAVIOR_VERBS:
        if v in joined.lower():
            # возьмём 8–12 слов вокруг
            m = re.search(rf"(.{{0,60}}{re.escape(v)}.{{0,60}})", joined, flags=re.I)
            if m: behavior = m.group(1)
            break
    behavior = behavior or joined[:120]

    parts = []
    if context:  parts.append(f"Когда {context.strip().rstrip('.')}")
    if behavior: parts.append(f"я {behavior.strip().rstrip('.')}")
    if emotions: parts.append(f"(обычно чувства: {emotions.strip().rstrip('.')})")
    s = " → ".join(parts) or joined[:160]
    # подчистим повторы
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _ask_confirm(chat_id: int, summary: str):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Да, верно", callback_data="sum_yes"),
        types.InlineKeyboardButton("Не совсем", callback_data="sum_no"),
    )
    bot.send_message(
        chat_id,
        f"Зафиксирую так:\n\n— *{summary}*\n\nВерно сформулировал?",
        reply_markup=kb
    )

# Переопределяем поведение на кнопку «У меня ошибка»
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error_soft(m):
    save_state(m.from_user.id, "error", {"phase": "free", "free_notes": [], "round": 0})
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями на *уровне поведения/навыка*.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

# Слушаем свободный рассказ в фазе "free"
@bot.message_handler(func=lambda msg: _load_state(msg.from_user.id).get("intent") == "error"
                               and (_load_state(msg.from_user.id).get("data") or {}).get("phase") == "free"
                               and msg.content_type == "text")
def error_free_collect(m):
    st = _load_state(m.from_user.id)
    data = st["data"]; rnd = int(data.get("round", 0))
    text_in = (m.text or "").strip()

    # накапливаем
    _push_note(m.from_user.id, text_in)
    rnd += 1
    _merge_state(m.from_user.id, round=rnd)

    # проверка на «размытость» — попросим конкретику
    if _has_vague(text_in):
        bot.send_message(
            m.chat.id,
            "Понял. Давай чуть конкретнее: *в какие именно дни/условиях* это случается? (например: «после серии стопов», «перед закрытием дня», «после новостей»)",
            reply_markup=main_menu()
        )
        return

    # если явно нет поведения — мягко подведём
    if not _has_behavior(text_in):
        bot.send_message(
            m.chat.id,
            "Уточни, пожалуйста, *что именно ты делаешь* в эти моменты (глаголами): «вхожу раньше», «двигаю стоп», «усредняю» и т.п.",
            reply_markup=main_menu()
        )
        return

    # достигли планового количества раундов — предложим сводку для подтверждения
    if rnd >= FREE_ROUNDS:
        notes = _load_state(m.from_user.id)["data"].get("free_notes", [])
        summary = _propose_summary(notes)
        _merge_state(m.from_user.id, summary=summary)
        _ask_confirm(m.chat.id, summary)
        return

    # иначе задаём следующий мягкий вопрос
    prompts = [
        "Где по времени это чаще случается? (утро/конец дня/после убыточной серии)",
        "Какие чувства всплывают сильнее всего в этот момент? (тревога/спешка/страх упустить и т.п.)",
        "Что предшествует ошибке? (нет сетапов долго, новости, желание «вырваться»)",
    ]
    bot.send_message(m.chat.id, prompts[min(rnd-1, len(prompts)-1)], reply_markup=main_menu())

# Обработка подтверждения сводки
@bot.callback_query_handler(func=lambda c: c.data in ("sum_yes", "sum_no"))
def on_summary_confirm(c):
    st = _load_state(c.from_user.id)
    data = st["data"]; summary = data.get("summary","").strip()
    if c.data == "sum_yes" and summary:
        # Done-условие выполнено → MERCEDES
        save_state(c.from_user.id, "error_mercedes", {"summary": summary})
        bot.edit_message_text(
            chat_id=c.message.chat.id, message_id=c.message.message_id,
            text=f"Принято ✅\n\n— *{summary}*\n\nПойдём коротко по MERCEDES, чтобы увидеть паттерн."
        )
        bot.send_message(
            c.message.chat.id,
            "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
            reply_markup=main_menu()
        )
    else:
        # попросим поправить формулу и вернём в free
        save_state(c.from_user.id, "error", {"phase": "free", "free_notes": data.get("free_notes", []), "round": 0})
        bot.edit_message_text(
            chat_id=c.message.chat.id, message_id=c.message.message_id,
            text="Ок, поправим формулировку. Что бы ты добавил/изменил, чтобы было точнее?"
        )

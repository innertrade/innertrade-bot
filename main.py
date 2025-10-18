# logic_layer.py — Innertrade Kai Mentor Bot (coach engine)
# v8.0 — когнитивный слой: медленная калибровка → резюме → подтверждение → переход к структуре

from __future__ import annotations
from typing import Dict, Any, List
from difflib import SequenceMatcher
import re
import json

# --- эвристики распознавания паттернов риска/эмоций (совпадает с main) ---
RISK_PATTERNS = {
    "remove_stop": ["убираю стоп", "снял стоп", "без стопа"],
    "move_stop": ["двигаю стоп", "отодвинул стоп", "переставил стоп"],
    "early_close": ["закрыл рано", "вышел в ноль", "мизерный плюс", "ранний выход"],
    "averaging": ["усреднение", "доливался против", "докупал против"],
    "fomo": ["поезд уедет", "упустил", "уйдёт без меня", "страх упустить"],
    "rule_breaking": ["нарушил план", "отошёл от плана", "игнорировал план"],
}
EMO_PATTERNS = {
    "self_doubt": ["сомневаюсь", "не уверен", "стресс", "паника", "волнение"],
    "fear_of_loss": ["страх потерь", "боюсь стопа", "не хочу быть обманутым"],
    "chaos": ["хаос", "суета", "путаюсь"],
}

BAN_TEMPLATES = [
    "понимаю", "это может быть", "важно понять", "давай рассмотрим", "было бы полезно",
    "попробуй", "используй", "придерживайся", "установи", "сфокусируйся", "следуй", "пересмотри"
]

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()

def strip_templates(text_in: str) -> str:
    t = text_in or ""
    for ph in BAN_TEMPLATES:
        t = re.sub(rf"(?i)\b{re.escape(ph)}[^.!?]*[.!?]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.!?")[:1200]
    return t

def detect_trading_patterns(text: str) -> List[str]:
    tl = (text or "").lower()
    hits = []
    for name, keys in {**RISK_PATTERNS, **EMO_PATTERNS}.items():
        if any(k in tl for k in keys):
            hits.append(name)
    return hits

def measure_clarity(history: List[Dict[str, str]]) -> float:
    """
    Простая оценка «конкретности»: наличие временных/ситуационных маркеров и действий.
    0.0 — абстракция, 1.0 — конкретный кейс.
    """
    txt = " ".join([m.get("content", "") for m in history if m.get("role") == "user"])[-1200:].lower()
    signals = 0
    for kw in ["вчера", "сегодня", "на днях", "на прошлой неделе", "на выходных",
               "когда", "тогда", "в момент", "после входа", "после открытия", "в сделке",
               "стоп", "тейк", "объём", "позиция", "вошёл", "закрыл", "открыл", "план", "сетап",
               "лонг", "шорт", "перенёс", "изменил", "поставил", "снял"]:
        if kw in txt:
            signals += 1
    return max(0.0, min(1.0, signals / 12.0))

def should_force_structural(text: str) -> bool:
    pats = detect_trading_patterns(text)
    risk = set(pats) & set(RISK_PATTERNS.keys())
    return bool(risk) or ("fear_of_loss" in pats) or ("self_doubt" in pats)

def extract_problem_summary(history: List[Dict]) -> str:
    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    pats: List[str] = []
    for m in user_msgs:
        pats.extend(detect_trading_patterns(m))
    up = sorted(set(pats))
    parts = []
    if "fomo" in up: parts.append("FOMO (страх упустить)")
    if "remove_stop" in up or "move_stop" in up: parts.append("трогаешь/снимаешь стоп")
    if "early_close" in up: parts.append("ранний выход/«в ноль»")
    if "averaging" in up: parts.append("усреднение против позиции")
    if "fear_of_loss" in up: parts.append("страх стопа/потерь")
    if "self_doubt" in up: parts.append("сомнения после входа")
    return "Триггеры: " + (", ".join(parts) if parts else "нужен пример")

# ----------------- GPT вызовы (два промпта) -----------------

def gpt_coach_explore(oai_client, model: str, style: str, history: List[Dict[str, str]], user_text: str) -> Dict[str, Any]:
    """
    Фаза медленной калибровки: уточняет и углубляет, не даёт советы, не зовёт структуру.
    """
    sys = f"""
Ты — Алекс, коуч-наставник по трейдингу. Общайся живо на «{style}».
Цель этой фазы — калибровка: уточнять, прояснять контекст, эмоции и мысли. Без советов.
Не упоминай названия техник. Короткие вопросы, отражение смысла.
Формат ответа — JSON с ключами:
response_text (коротко и разговорно), store (объект, можно пустой).
""".strip()

    msgs = [{"role": "system", "content": sys}]
    for h in history[-16:]:
        if h.get("role") in ("user", "assistant"):
            msgs.append(h)
    msgs.append({"role": "user", "content": user_text})

    try:
        res = oai_client.chat.completions.create(
            model=model,
            messages=msgs,
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        data = json.loads(raw)
        text = strip_templates(data.get("response_text", "")) or "Давай чуть конкретнее: опиши последний момент, где всё пошло в сторону."
        data["response_text"] = text
        if "store" not in data or not isinstance(data["store"], dict):
            data["store"] = {}
        return data
    except Exception:
        return {"response_text": "Окей, уточню: где и когда это было, и что именно ты сделал?", "store": {}}

def gpt_coach_summarize(oai_client, model: str, style: str, history: List[Dict[str, str]], user_text: str) -> Dict[str, Any]:
    """
    Фаза резюме: кратко формулирует проблему пользователем и просит подтверждение.
    """
    sys = f"""
Ты — Алекс, коуч-наставник по трейдингу. Общайся на «{style}».
Твоя задача — кратко суммировать формулировку проблемы словами пользователя и попросить подтверждения.
Не давай советы. Не упоминай технику. Если формулировка сырая — мягко отметь, что ещё чуть-чуть уточним.
Формат ответа — JSON: response_text (строка), propose_summary (строка), ask_confirm (bool).
""".strip()

    msgs = [{"role": "system", "content": sys}]
    for h in history[-16:]:
        if h.get("role") in ("user", "assistant"):
            msgs.append(h)
    msgs.append({"role": "user", "content": user_text})

    try:
        res = oai_client.chat.completions.create(
            model=model,
            messages=msgs,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = res.choices[0].message.content or "{}"
        data = json.loads(raw)
        rt = data.get("response_text", "")
        pr = data.get("propose_summary", "")
        ac = bool(data.get("ask_confirm", False))
        if not pr:
            ac = False
        data["response_text"] = strip_templates(rt) or "Похоже, мы близко. Скажи, если кратко — в чём именно затык сейчас?"
        data["propose_summary"] = pr
        data["ask_confirm"] = ac
        return data
    except Exception:
        return {"response_text": "Соберу в одну строчку и сверимся, окей?", "propose_summary": "", "ask_confirm": False}

# ----------------- Главный переключатель цикла -----------------

def process_turn(
    oai_client,
    model: str,
    state: Dict[str, Any],
    user_text: str
) -> Dict[str, Any]:
    """
    Возвращает:
      - reply (str)
      - state_updates (dict)
      - ask_confirm (bool)
      - propose_summary (str)
      - suggest_struct (bool)
    """
    updates: Dict[str, Any] = {}
    history: List[Dict[str, str]] = state.get("history", [])
    style = state.get("style", "ты")

    # обновим метрики
    clarity = measure_clarity(history)
    updates.setdefault("coach", {})
    coach = updates["coach"]
    prev = state.get("coach", {})
    coach["clarity"] = round(clarity, 2)
    turn = int(prev.get("turns", 0)) + 1
    coach["turns"] = turn
    loop = prev.get("loop") or "explore"  # explore -> summarize -> structure
    coach["loop"] = loop

    # эвристика переходов
    force_struct = should_force_structural(user_text)
    ask_confirm = False
    propose_summary = ""
    suggest_struct = False

    # если уже подтверждено — сразу структура
    if state.get("problem_confirmed"):
        coach["loop"] = "structure"

    if coach["loop"] == "explore":
        # медленная калибровка не менее 3 ходов или пока clarity < 0.55
        data = gpt_coach_explore(oai_client, model, style, history, user_text)
        reply = data["response_text"]
        updates.update(data.get("store", {}))
        # условия выхода в summarize
        if force_struct or (turn >= 3 and clarity >= 0.55):
            coach["loop"] = "summarize"

    elif coach["loop"] == "summarize":
        data = gpt_coach_summarize(oai_client, model, style, history, user_text)
        reply = data["response_text"]
        propose_summary = data.get("propose_summary", "")
        ask_confirm = bool(data.get("ask_confirm", False))
        if ask_confirm and propose_summary:
            # ждём подтверждения от пользователя кнопкой
            pass
        else:
            # пока продолжаем уточнять
            coach["loop"] = "explore"

    else:  # structure
        suggest_struct = True
        reply = "Готов пройтись по шагам и собрать краткий план изменений?"

    return {
        "reply": reply,
        "state_updates": updates,
        "ask_confirm": ask_confirm,
        "propose_summary": propose_summary,
        "suggest_struct": suggest_struct,
    }

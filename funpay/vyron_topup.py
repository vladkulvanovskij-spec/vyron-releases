"""
Vyron OS: пополнение баланса за заказы FunPay.

Как работает:
1. Покупатель оплачивает лот, название которого есть в LOTS ниже.
2. Бот пишет ему в чат заказа: «Пришлите email аккаунта Vyron OS».
3. Покупатель отвечает email-ом. Плагин отправляет его на сайт Vyron (функция funpay-topup),
   и на баланс аккаунта зачисляется сумма из LOTS. Один заказ зачисляется только один раз
   (это проверяет сам сайт), поэтому повтор сообщения ничего не удвоит.
4. Покупатель заходит в Vyron OS и покупает на баланс нужную подписку.

Установка: положите файл в папку plugins Кардинала (или отправьте боту в Telegram: /menu →
Плагины → Добавить плагин), впишите SECRET (тот же, что FUNPAY_TOPUP_SECRET на сайте) и перезапустите.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from logging import getLogger

import requests

NAME = "Vyron OS Top-up"
VERSION = "1.0.0"
DESCRIPTION = "Зачисляет баланс Vyron OS покупателю после оплаты лота на FunPay."
CREDITS = "Vyron OS"
UUID = "3f6c2b1e-8d4a-4c3b-9e7f-5a1d2c3b4e5f"
SETTINGS_PAGE = False

logger = getLogger("FPC.vyron_topup")

# ── настройки ──────────────────────────────────────────────────────────────────────────────
API_URL = "https://uvhmljcvxwfwloiddosa.supabase.co/functions/v1/funpay-topup"
SECRET = ""   # пусто = берётся из storage/plugins/vyron_config.json (его создаёт vyron_updater.py)

# Часть названия лота (без учёта регистра) → сколько долларов зачислить на баланс.
LOTS = {
    # лоты из vyron_lots.py (русские и английские названия)
    "ELITE ОПТИМИЗАЦИЯ": 69.99,           # хватает на подписку «Навсегда»
    "КИБЕРСПОРТ ОПТИМИЗАЦИЯ": 29.99,      # на год
    "ОПТИМИЗАЦИЯ ПК ДЛЯ": 4.99,           # «ОПТИМИЗАЦИЯ ПК ДЛЯ <игры> ОДНОЙ КНОПКОЙ», на месяц
    "CYBERSPORT OPTIMIZATION": 29.99,
    "ONE-CLICK PC OPTIMIZATION": 4.99,
    # прежние названия
    "ELITE PC OPTIMIZATION": 69.99,
    "CYBERSPORT PC OPTIMIZATION": 29.99,
    "PRO PC OPTIMIZATION": 4.99,
}

MAX_TRIES = 3          # сколько раз можно прислать неверный email
STATE_FILE = os.path.join("storage", "plugins", "vyron_topup.json")

ASK = ("Спасибо за покупку! Чтобы зачислить баланс в Vyron OS, пришлите сюда одним сообщением "
       "email, с которым вы вошли в приложение или на сайт vyron-auth.vercel.app. "
       "Если аккаунта ещё нет, зарегистрируйтесь там и пришлите email.")
DONE = ("Готово: на баланс {email} зачислено ${amount}. Откройте Vyron OS → Подписка и купите тариф. "
        "Если что-то не так, напишите сюда.")
ALREADY = "Этот заказ уже зачислен на {email}. Баланс виден в Vyron OS и на сайте."
NO_USER = ("Аккаунт с email {email} не найден. Проверьте адрес (тот, с которым вы входите в Vyron OS) "
           "и пришлите его ещё раз.")
GIVE_UP = "Не получилось найти аккаунт. Продавец скоро подключится и поможет вручную."
LATER = "Сайт сейчас не отвечает. Я повторю зачисление сам через пару минут, ничего делать не нужно."

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pending": {}, "done": {}}


def _save(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _secret() -> str:
    if SECRET:
        return SECRET
    try:
        with open(os.path.join("storage", "plugins", "vyron_config.json"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("FUNPAY_TOPUP_SECRET", ""))
    except Exception:
        return ""


def _amount_for(description: str) -> float | None:
    d = (description or "").upper()
    for key, amount in LOTS.items():
        if key.upper() in d:
            return amount
    return None


def _credit(order_id: str, email: str, amount: float, buyer: str) -> tuple[str, dict]:
    """→ ("ok" | "already" | "no_user" | "retry" | "error", body)"""
    try:
        r = requests.post(API_URL, timeout=15, headers={"x-funpay-secret": _secret(), "Content-Type": "application/json"},
                          json={"order_id": order_id, "email": email, "amount_usd": amount, "buyer": buyer})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as ex:
        logger.warning(f"[vyron_topup] сайт не ответил по заказу #{order_id}: {ex}")
        return "retry", {}
    if r.status_code == 200 and body.get("ok"):
        return ("already" if body.get("already") else "ok"), body
    if body.get("reason") == "no_user":
        return "no_user", body
    if r.status_code >= 500:
        return "retry", body
    logger.error(f"[vyron_topup] заказ #{order_id} отклонён: {r.status_code} {body}")
    return "error", body


def on_new_order(c, e, *args):
    order = e.order
    amount = _amount_for(order.description)
    if amount is None:
        return
    if not _secret():
        logger.error("[vyron_topup] секрет не задан: установите vyron_updater.py (он создаёт vyron_config.json)")
        return
    with _lock:
        state = _load()
        if order.id in state["done"] or order.id in state["pending"]:
            return
        state["pending"][order.id] = {"chat_id": order.chat_id, "buyer": order.buyer_username, "buyer_id": order.buyer_id,
                                      "amount": amount, "tries": 0, "email": None, "at": int(time.time())}
        _save(state)
    c.send_message(order.chat_id, ASK, order.buyer_username)
    logger.info(f"[vyron_topup] заказ #{order.id}: жду email от {order.buyer_username} (${amount})")


def _finish(c, order_id: str, p: dict, email: str):
    status, body = _credit(order_id, email, p["amount"], p["buyer"])
    with _lock:
        state = _load()
        cur = state["pending"].get(order_id)
        if cur is None:
            return
        if status in ("ok", "already"):
            state["pending"].pop(order_id, None)
            state["done"][order_id] = {"email": email, "amount": p["amount"], "at": int(time.time())}
            _save(state)
            text = (DONE if status == "ok" else ALREADY).format(email=email, amount=p["amount"])
        elif status == "no_user":
            cur["tries"] += 1
            cur["email"] = None
            give_up = cur["tries"] >= MAX_TRIES
            if give_up:
                state["pending"].pop(order_id, None)
                state.setdefault("manual", {})[order_id] = cur
            _save(state)
            text = GIVE_UP if give_up else NO_USER.format(email=email)
        elif status == "retry":
            cur["email"] = email     # the retry loop picks it up
            first = not cur.get("told_later")
            cur["told_later"] = True
            _save(state)
            text = LATER if first else None
        else:
            state["pending"].pop(order_id, None)
            state.setdefault("manual", {})[order_id] = cur
            _save(state)
            text = GIVE_UP
    if text:
        c.send_message(p["chat_id"], text, p["buyer"])
    logger.info(f"[vyron_topup] заказ #{order_id}: {status} ({email})")


def on_new_message(c, e, *args):
    m = e.message
    if m.by_bot or m.author_id == c.account.id or m.author_id == 0:
        return
    found = EMAIL_RE.search(m.text or "")
    if not found:
        return
    with _lock:
        state = _load()
        # the oldest waiting order of this buyer in this chat
        hits = [(k, v) for k, v in state["pending"].items() if str(v["chat_id"]) == str(m.chat_id) and v.get("email") is None]
        if not hits:
            return
        order_id, p = sorted(hits, key=lambda kv: kv[1]["at"])[0]
    threading.Thread(target=_finish, args=(c, order_id, p, found.group(0).lower()), daemon=True).start()


def _retry_loop(c):
    while True:
        time.sleep(120)
        try:
            with _lock:
                waiting = [(k, dict(v)) for k, v in _load()["pending"].items() if v.get("email")]
            for order_id, p in waiting:
                _finish(c, order_id, p, p["email"])
        except Exception as ex:
            logger.warning(f"[vyron_topup] повтор не удался: {ex}")


def on_post_start(c, *args):
    threading.Thread(target=_retry_loop, args=(c,), daemon=True).start()


BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_POST_START = [on_post_start]
BIND_TO_DELETE = None

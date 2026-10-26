"""
Vyron OS: выставить лоты оптимизации во все нужные разделы FunPay одной командой.

Команды в Telegram-боте Кардинала (только для авторизованных пользователей бота):
  /vyron_lots_backup        сохранить копию ВСЕХ ваших лотов (поля формы) в storage/plugins/vyron_lots_backup_*.json
  /vyron_lots_preview       показать, какие лоты будут удалены и какие созданы (ничего не меняет)
  /vyron_lots_apply YES     сделать копию, удалить старые лоты оптимизации и выставить новые
  /vyron_lots_restore       вернуть лоты из последней копии (создаёт их заново)

Тексты и лимиты: длину полей плагин читает из формы FunPay (maxlength) и собирает описание из блоков
по важности, пока они помещаются, так что текст никогда не обрывается на полуслове.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from logging import getLogger

NAME = "Vyron OS Lots"
VERSION = "1.0.0"
DESCRIPTION = "Выставляет лоты Vyron OS во все разделы оптимизации FunPay (с копией и откатом)."
CREDITS = "Vyron OS"
UUID = "8b2e5d71-4c9a-4f36-b1d2-7e0a9c3f6a18"
SETTINGS_PAGE = False

logger = getLogger("FPC.vyron_lots")

# ── разделы FunPay (node id → игра) ───────────────────────────────────────────────────────────
NODES = {
    1351: "CS2",            # Counter-Strike 2 → Прочее
    613: "Valorant",        # Valorant → Прочее
    1669: "Valorant",       # Valorant → Услуги
    504: "Dota 2",          # Dota 2 → Прочее
    249: "Fortnite",        # Fortnite → Услуги
    871: "PUBG",            # PUBG → Прочее
    2046: "любых игр",      # Другие игры → Прочее
    453: "любых игр",       # Другие игры → Услуги
}

# Лоты, которые НЕ удаляются (часть названия, без учёта регистра)
KEEP = ["PC OPTIMIZATION | FPS BOOST + LOW LATENCY"]
# Что считается «вашим лотом оптимизации» для удаления (часть названия, без учёта регистра)
OPTIMIZATION_WORDS = ["OPTIMIZATION", "ОПТИМИЗАЦ", "БУСТ", "BOOST", "INPUT LAG", "FPS", "ФПС", "НАСТРОЙК"]

SITE = "vyron-auth.vercel.app"
PAUSE = 3.0          # секунд между запросами к FunPay, чтобы не словить ограничение

# ── тарифы ─────────────────────────────────────────────────────────────────────────────────────
TIERS = [
    {"key": "elite", "price": 3666, "balance": "69.99", "plan_ru": "НАВСЕГДА", "plan_en": "LIFETIME",
     "title_ru": "⚡ ELITE ОПТИМИЗАЦИЯ ПК {g} | ЗАМЕР FPS ДО/ПОСЛЕ | РАЗГОН GPU | ОТКАТ 1 КЛИК | ПОДПИСКА НАВСЕГДА",
     "title_en": "⚡ ELITE PC OPTIMIZATION {g} | FPS BEFORE/AFTER | GPU OC | 1-CLICK UNDO | LIFETIME"},
    {"key": "cyber", "price": 1527, "balance": "29.99", "plan_ru": "НА ГОД", "plan_en": "1 YEAR",
     "title_ru": "🏆 КИБЕРСПОРТ ОПТИМИЗАЦИЯ {g} | -INPUT LAG | MAX FPS | ЗАМЕР ДО/ПОСЛЕ | ГАРАНТИЯ | ГОД",
     "title_en": "🏆 CYBERSPORT OPTIMIZATION {g} | -INPUT LAG | MAX FPS | BEFORE/AFTER | GUARANTEE | 1 YEAR"},
    {"key": "pro", "price": 477.65, "balance": "4.99", "plan_ru": "НА МЕСЯЦ", "plan_en": "1 MONTH",
     "title_ru": "🚀 ОПТИМИЗАЦИЯ ПК {g} ОДНОЙ КНОПКОЙ | +FPS -ФРИЗЫ -INPUT LAG | ЗАМЕР ДО/ПОСЛЕ | WIN10/11",
     "title_en": "🚀 ONE-CLICK PC OPTIMIZATION {g} | +FPS -STUTTER -INPUT LAG | BEFORE/AFTER | WIN10/11"},
]

# ── описание: блоки по важности (первые всегда, остальные пока помещаются) ───────────────────────
def blocks_ru(t: dict, game: str) -> list[str]:
    g = "в " + game if game != "любых игр" else "в любых играх"
    head = {
        "elite": f"⚡ ELITE — максимум, что можно выжать из вашего ПК {g}, с замером результата и подпиской Vyron OS НАВСЕГДА.",
        "cyber": f"🏆 КИБЕРСПОРТ — минимальная задержка и ровные кадры {g}, с замером результата и подпиской Vyron OS НА ГОД.",
        "pro": f"🚀 PRO — быстрая оптимизация ПК {g} одной кнопкой, с замером результата и подпиской Vyron OS НА МЕСЯЦ.",
    }[t["key"]]
    out = [
        (0, head),
        (1, "❗ Это не гайд и не пак сомнительных твиков. Оптимизацию делает программа Vyron OS: каждое изменение описано, "
        "проверяется и отменяется одной кнопкой, перед изменениями создаётся точка восстановления."),
        (2, "📦 КАК ПОЛУЧИТЬ\n"
        "1) Оплачиваете лот.\n"
        "2) Бот сразу попросит email вашего аккаунта Vyron OS — пришлите его в чат. Нет аккаунта? Регистрация за минуту на " + SITE + ".\n"
        f"3) На баланс автоматически приходит ${t['balance']}: в Vyron OS → Подписка выбираете тариф {t['plan_ru']}.\n"
        "4) Жмёте «Оптимизировать всё» — программа сама проходит все этапы и показывает отчёт."),
        (3, "📊 ЧЕСТНЫЙ РЕЗУЛЬТАТ\nVyron сам запускает игру, меряет FPS и 1% low ДО и ПОСЛЕ и показывает настоящую разницу. "
        "Мы не пишем «+500 FPS»: прирост зависит от железа, и программа покажет его цифрами на вашем ПК."),
        (4, "🛡 ГАРАНТИЯ\n• Любое изменение отменяется одной кнопкой.\n• Если программа не запускается на вашем ПК — верну деньги.\n"
        "• Вопросы и помощь — прямо в этом чате."),
    ]
    feats = [
        "⚙️ WINDOWS: 167 настроек — фоновые процессы, телеметрия, службы, игровой режим, планировщик, системный таймер, "
        "очистка мусора, автозагрузка, лишние программы (Discord, Edge, Steam в фоне).",
        "🎮 ВО ВРЕМЯ ИГРЫ: приоритет игре, приглушение фона, свой план питания; игра больше не замедляется энергосбережением Windows. "
        "Соревновательный режим включается сам, когда запускается игра.",
        "🖥 ВИДЕОКАРТА: профиль NVIDIA, соревновательный профиль AMD, кэп FPS и Reflex в CS2 и Fortnite, "
        "чистая переустановка драйвера без DDU, драйверы всех устройств.",
        "🌐 СЕТЬ: тест пинга под нагрузкой (bufferbloat), джиттер и потери, быстрый DNS, настройка сетевой карты с замером пинга.",
        "🎯 ИГРЫ: пресеты запуска CS2, Dota 2, Rust, Roblox; правила для игр (приоритет, ядра); поиск того, что ограничивает ваш FPS.",
    ]
    if t["key"] in ("elite", "cyber"):
        feats.insert(2, "🔥 РАЗГОН: авторазгон и андервольт видеокарт AMD, лимит мощности NVIDIA, проверка XMP памяти. "
                        "Сначала 3-минутная проверка стабильности, при сбое драйвера всё возвращается само.")
    out.insert(2, (5, ["✅ ЧТО ДЕЛАЕТ VYRON OS"] + feats))   # shown second, filled after the essentials; lines added while they fit
    if t["key"] == "elite":
        out.append("👤 ЛИЧНО: по желанию помогу удалённо (AnyDesk/Discord) — подскажу, что включить именно на вашем ПК.")
    out.append("💻 Windows 10/11 · NVIDIA · AMD · Intel · ПК и ноутбуки" + (" (на ноутбуке разгон ограничен тихими режимами ради охлаждения)." if t["key"] != "pro" else "."))
    out.append("❓ Не уверены, какой тариф? Напишите модель процессора и видеокарты — подскажу честно, что даст.")
    return out


def blocks_en(t: dict, game: str) -> list:
    """The same blocks, order and priorities as blocks_ru, translated."""
    g = "in " + game if game != "любых игр" else "in any game"
    head = {
        "elite": f"⚡ ELITE — the most your PC can give {g}, with a measured result and a LIFETIME Vyron OS subscription.",
        "cyber": f"🏆 CYBERSPORT — the lowest input delay and smooth frames {g}, with a measured result and a 1-YEAR Vyron OS subscription.",
        "pro": f"🚀 PRO — fast one-click PC optimization {g}, with a measured result and a 1-MONTH Vyron OS subscription.",
    }[t["key"]]
    out = [
        (0, head),
        (1, "❗ Not a guide and not a pack of dubious tweaks. The optimization is done by the Vyron OS app: every change is described, "
        "checked and undone with one click, and a restore point is created first."),
        (2, "📦 HOW TO GET IT\n"
        "1) Pay for the lot.\n"
        "2) The bot will ask for the email of your Vyron OS account — send it in the chat. No account? Sign up in a minute at " + SITE + ".\n"
        f"3) ${t['balance']} is added to your balance automatically: in Vyron OS → Subscription choose the {t['plan_en']} plan.\n"
        "4) Press “Optimize everything” — the app goes through every step by itself and shows a report."),
        (3, "📊 HONEST RESULT\nVyron starts the game itself, measures FPS and 1% low BEFORE and AFTER and shows the real difference. "
        "We don't write “+500 FPS”: the gain depends on your hardware, and the app shows it in numbers on your PC."),
        (4, "🛡 GUARANTEE\n• Any change is undone with one click.\n• If the app does not run on your PC — I refund the money.\n"
        "• Questions and help — right here in this chat."),
    ]
    feats = [
        "⚙️ WINDOWS: 167 tweaks — background processes, telemetry, services, Game Mode, scheduler, system timer, "
        "junk cleanup, startup, apps running in the background (Discord, Edge, Steam).",
        "🎮 WHILE YOU PLAY: priority for the game, background calmed down, its own power plan; Windows power saving no longer slows the game. "
        "Competitive mode turns on by itself when the game starts.",
        "🖥 GRAPHICS CARD: NVIDIA profile, AMD competitive profile, FPS cap and Reflex in CS2 and Fortnite, "
        "clean driver reinstall without DDU, drivers for every device.",
        "🌐 NETWORK: ping test under load (bufferbloat), jitter and packet loss, fast DNS, network card tuning with a ping test.",
        "🎯 GAMES: launch presets for CS2, Dota 2, Rust, Roblox; rules for games (priority, cores); finds what limits your FPS.",
    ]
    if t["key"] in ("elite", "cyber"):
        feats.insert(2, "🔥 OVERCLOCKING: AMD GPU auto-overclock and undervolt, NVIDIA power limit, XMP memory check. "
                        "A 3-minute stability test first; if the driver crashes, everything goes back by itself.")
    out.insert(2, (5, ["✅ WHAT VYRON OS DOES"] + feats))
    if t["key"] == "elite":
        out.append("👤 PERSONAL: if you want, I help remotely (AnyDesk/Discord) — what to turn on for your exact PC.")
    out.append("💻 Windows 10/11 · NVIDIA · AMD · Intel · desktops and laptops" + (" (on a laptop overclocking is limited to quiet modes for cooling)." if t["key"] != "pro" else "."))
    out.append("❓ Not sure which plan? Send your CPU and graphics card models — I'll tell you honestly what it gives.")
    return out


def fit(blocks: list, limit: int | None, sep: str = "\n\n") -> str:
    """Picks blocks by priority (an item may be (priority, block); plain items get their position) and prints the picked
    ones in their original order, so a short limit keeps «how to get it» and drops the extras, never cutting mid-sentence.
    A block that is a list adds its lines one by one (its first line is a header). The first block is always kept."""
    items = [(b[0], i, b[1]) if isinstance(b, tuple) else (10 + i, i, b) for i, b in enumerate(blocks)]
    chosen: dict[int, str] = {}

    def render(extra: dict | None = None) -> str:
        d = dict(chosen)
        d.update(extra or {})
        return sep.join(d[k] for k in sorted(d))

    for prio, idx, b in sorted(items, key=lambda x: (x[0], x[1])):
        if isinstance(b, list):
            part = ""
            for j, line in enumerate(b):
                cand = line if not part else part + "\n" + line
                if limit is None or len(render({idx: cand})) <= limit:
                    part = cand
                elif j == 0:
                    break
            if part and part != b[0]:
                chosen[idx] = part
            continue
        if limit is None or len(render({idx: b})) <= limit:
            chosen[idx] = b
        elif not chosen:
            return b[: max(0, limit - 1)].rsplit(" ", 1)[0] + "…"
    return render()

def fit_title(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    parts = [p.strip() for p in text.split("|")]
    while len(parts) > 1 and len(" | ".join(parts)) > limit:
        parts.pop()      # drop the least important trailing segment
    s = " | ".join(parts)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ── FunPay ─────────────────────────────────────────────────────────────────────────────────────
def _limits(c, node: int) -> dict:
    """maxlength of the text fields, read from the lot form (None = not stated)."""
    lim = {}
    try:
        html = c.account.method("get", f"lots/offerEdit?node={node}", {}, {}, raise_not_200=True).text
        for key in ("summary", "desc", "payment_msg"):
            for lang in ("ru", "en"):
                m = re.search(r'name="fields\[' + key + r'\]\[' + lang + r'\]"[^>]*?maxlength="(\d+)"', html) or \
                    re.search(r'maxlength="(\d+)"[^>]*?name="fields\[' + key + r'\]\[' + lang + r'\]"', html)
                lim[f"{key}_{lang}"] = int(m.group(1)) if m else None
    except Exception as ex:
        logger.warning(f"[vyron_lots] лимиты для раздела {node} не прочитаны: {ex}")
    return lim


def _my_lots(c):
    profile = c.account.get_user(c.account.id)
    return profile.get_lots()


def _is_optimization(title: str) -> bool:
    t = (title or "").upper()
    if any(k.upper() in t for k in KEEP):
        return False
    return any(w in t for w in OPTIMIZATION_WORDS)


def _storage(name: str) -> str:
    d = os.path.join("storage", "plugins")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def backup(c) -> tuple[str, int]:
    saved = []
    for lot in _my_lots(c):
        try:
            f = c.account.get_lot_fields(lot.id)
            node = getattr(getattr(lot, "subcategory", None), "id", None)
            saved.append({"id": lot.id, "node": node, "title": lot.description, "fields": f.fields})
        except Exception as ex:
            logger.warning(f"[vyron_lots] копия лота {lot.id} не снята: {ex}")
        time.sleep(PAUSE / 2)
    path = _storage(f"vyron_lots_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(saved, fh, ensure_ascii=False, indent=1)
    return path, len(saved)


def plan(c) -> tuple[list, list]:
    """(lots to delete, [(node, game, tier)] to create)"""
    lots = _my_lots(c)
    delete = [l for l in lots if _is_optimization(l.description)]
    create = [(node, game, t) for node, game in NODES.items() for t in TIERS]
    return delete, create


def _template(c, node: int, mine: dict):
    """Fields for a new lot in `node`: a copy of one of my lots there (keeps the section's own select fields), else the empty form."""
    from FunPayAPI import types
    if node in mine:
        f = c.account.get_lot_fields(mine[node])
        fields = dict(f.fields)
        fields["offer_id"] = "0"
        return types.LotFields(0, fields, f.subcategory, f.currency)
    return c.account.get_lot_fields(0, node)


def apply(c, report) -> None:
    path, n = backup(c)
    report(f"Копия {n} лотов: {path}")
    delete, create = plan(c)
    mine = {}
    for l in _my_lots(c):
        node = getattr(getattr(l, "subcategory", None), "id", None)
        if node in NODES and node not in mine:
            mine[node] = l.id
    templates = {}
    for node in NODES:      # take the templates BEFORE deleting anything
        try:
            templates[node] = _template(c, node, mine)
        except Exception as ex:
            report(f"⚠️ Раздел {node}: форма не получена ({ex}), пропускаю")
        time.sleep(PAUSE / 2)
    created = failed = 0
    for node, game, t in create:
        if node not in templates:
            continue
        lim = _limits(c, node)
        try:
            from FunPayAPI import types
            base = templates[node]
            lf = types.LotFields(0, dict(base.fields), base.subcategory, base.currency)
            lf.title_ru = fit_title(t["title_ru"].format(g="ДЛЯ " + (game.upper() if game != "любых игр" else "ЛЮБЫХ ИГР")), lim.get("summary_ru"))
            lf.title_en = fit_title(t["title_en"].format(g="FOR " + (game.upper() if game != "любых игр" else "ANY GAME")), lim.get("summary_en"))
            lf.description_ru = fit(blocks_ru(t, game), lim.get("desc_ru"))
            lf.description_en = fit(blocks_en(t, game), lim.get("desc_en"))
            lf.payment_msg_ru = fit(["Спасибо за покупку! Пришлите сюда email вашего аккаунта Vyron OS — баланс зачислится автоматически."], lim.get("payment_msg_ru"))
            lf.payment_msg_en = fit(["Thank you! Send the email of your Vyron OS account here — the balance is added automatically."], lim.get("payment_msg_en"))
            lf.price = t["price"]
            lf.active = True
            if lf.amount is None and not lf.auto_delivery:
                lf.amount = 999
            lf.renew_fields()
            c.account.save_lot(lf)
            created += 1
        except Exception as ex:
            failed += 1
            report(f"⚠️ Не создан {t['key']} в разделе {node}: {ex}")
        time.sleep(PAUSE)
    deleted = 0
    for l in delete:
        try:
            c.account.delete_lot(l.id)
            deleted += 1
        except Exception as ex:
            report(f"⚠️ Лот {l.id} не удалён: {ex}")
        time.sleep(PAUSE)
    report(f"Готово: создано {created}, не создано {failed}, удалено старых {deleted}. Копия: {path}")


def restore(c, report) -> None:
    from FunPayAPI import types
    files = sorted(glob.glob(_storage("vyron_lots_backup_*.json")))
    if not files:
        report("Копий нет.")
        return
    with open(files[-1], "r", encoding="utf-8") as fh:
        saved = json.load(fh)
    current = {(l.description or "").strip() for l in _my_lots(c)}
    done = 0
    for s in saved:
        if (s.get("title") or "").strip() in current:
            continue       # still there
        try:
            fields = dict(s["fields"])
            fields["offer_id"] = "0"
            fields.pop("deleted", None)
            fields["csrf_token"] = c.account.csrf_token
            c.account.save_lot(types.LotFields(0, fields))
            done += 1
        except Exception as ex:
            report(f"⚠️ «{s.get('title')}» не восстановлен: {ex}")
        time.sleep(PAUSE)
    report(f"Восстановлено лотов: {done} из копии {files[-1]}")


# ── Telegram ───────────────────────────────────────────────────────────────────────────────────
def init(c, *args):
    tg = c.telegram
    if tg is None:
        return
    bot = tg.bot

    def allowed(m) -> bool:
        return m.from_user.id in tg.authorized_users

    def say(m):
        return lambda text: bot.send_message(m.chat.id, text[:4000])

    def cmd_backup(m):
        if not allowed(m):
            return
        path, n = backup(c)
        bot.send_message(m.chat.id, f"Сохранено лотов: {n}\n{path}")

    def cmd_preview(m):
        if not allowed(m):
            return
        delete, create = plan(c)
        lines = [f"Будет удалено ({len(delete)}):"] + [f"• {l.description} — {l.price}" for l in delete]
        lines += ["", f"Будет создано ({len(create)}):"]
        for node, game, t in create:
            lines.append(f"• [{node} {game}] {t['key']} — {t['price']} ₽")
        lim = _limits(c, next(iter(NODES)))
        lines += ["", f"Лимиты формы (раздел {next(iter(NODES))}): {lim}",
                  "", "Применить: /vyron_lots_apply YES  (перед этим будет сделана копия всех лотов)"]
        bot.send_message(m.chat.id, "\n".join(lines)[:4000])

    def cmd_apply(m):
        if not allowed(m):
            return
        if "YES" not in (m.text or "").upper().split():
            bot.send_message(m.chat.id, "Чтобы применить, отправьте: /vyron_lots_apply YES")
            return
        bot.send_message(m.chat.id, "Начинаю: копия → новые лоты → удаление старых. Это займёт несколько минут.")
        try:
            apply(c, say(m))
        except Exception as ex:
            bot.send_message(m.chat.id, f"Ошибка: {ex}")

    def cmd_restore(m):
        if not allowed(m):
            return
        try:
            restore(c, say(m))
        except Exception as ex:
            bot.send_message(m.chat.id, f"Ошибка: {ex}")

    tg.msg_handler(cmd_backup, commands=["vyron_lots_backup"])
    tg.msg_handler(cmd_preview, commands=["vyron_lots_preview"])
    tg.msg_handler(cmd_apply, commands=["vyron_lots_apply"])
    tg.msg_handler(cmd_restore, commands=["vyron_lots_restore"])
    c.add_telegram_commands(UUID, [
        ("vyron_lots_backup", "Vyron: копия всех лотов", True),
        ("vyron_lots_preview", "Vyron: что удалится и создастся", True),
        ("vyron_lots_apply", "Vyron: выставить лоты (YES)", True),
        ("vyron_lots_restore", "Vyron: вернуть лоты из копии", True),
    ])


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None

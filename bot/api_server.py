from aiohttp import web
import json
import base64
import hmac
import hashlib
import urllib.parse
import random
import os
import time
from dotenv import load_dotenv
import logging
import aiohttp
from database_neon import Database
import cases_config
from settings import (
    ADMIN_USERNAMES, REQUIRED_CHANNEL, CHANNEL_SUB_URL, TELEGRAM_API_BASE,
    is_admin_username, is_maintenance_whitelisted,
)

logger = logging.getLogger(__name__)

load_dotenv()
NEON_CONNECTION_STRING = os.getenv("NEON_CONNECTION_STRING")
if not NEON_CONNECTION_STRING:
    raise ValueError("❌ NEON_CONNECTION_STRING не задана в .env файле!")

db = Database(NEON_CONNECTION_STRING)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан в переменных окружения!")


def verify_telegram_data(init_data: str) -> dict | None:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        check_hash = parsed.pop("hash", "")
        if not check_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, check_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if auth_date and time.time() - auth_date > 86400:
            return None
        user_data = json.loads(parsed.get("user", "{}"))
        if not user_data.get("id"):
            return None
        return user_data
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def verify_launch_token(token: str) -> dict | None:
    """Verify a signed token issued by the bot for WebApp keyboard launches."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        user_id_s, ts_s, sig = raw.split(":", 2)
        user_id = int(user_id_s)
        ts = int(ts_s)
        if user_id <= 0 or abs(time.time() - ts) > 86400:
            return None
        payload = f"{user_id}:{ts}"
        expected = hmac.new(BOT_TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        return {"id": user_id}
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def get_authenticated_user(request, body=None, allow_blocked=False):
    """Authenticate via Telegram initData or a bot-signed launch token.
    By default заблокированные пользователи не проходят API-аутентификацию.
    /api/user использует allow_blocked=True, чтобы Mini App мог показать экран ЧС.
    """
    user_data = verify_telegram_data(request.headers.get("X-Init-Data", ""))
    if not user_data:
        user_data = verify_launch_token(request.headers.get("X-Launch-Token", ""))
    if not user_data:
        return None
    if body is not None and body.get("user_id") is not None:
        try:
            if int(body["user_id"]) != int(user_data["id"]):
                return None
        except (ValueError, TypeError):
            return None
    if not allow_blocked and db.is_user_blocked(int(user_data["id"])):
        return {"id": int(user_data["id"]), "_blocked": True}
    return user_data


# ============================================================
# ДОСТУП: чёрный список, техработы, обязательная подписка
# ============================================================
# Эти пути доступны всегда: мини-аппу нужно узнать, что именно его не пускает.
OPEN_PATHS = {"/api/user", "/api/subscription/check"}

_sub_cache = {}          # user_id -> (is_member, checked_at)
_SUB_TTL_OK = 120        # подписан — не дёргаем Telegram 2 минуты
_SUB_TTL_NO = 5          # не подписан — перепроверяем почти сразу
_sub_error_logged_at = 0.0


def resolve_username(user_data: dict) -> str:
    """username из initData, а если запуск был по launch-token — из БД."""
    name = (user_data or {}).get("username") or ""
    if name:
        return name
    row = db.get_user(int(user_data["id"])) if user_data else None
    return (row or {}).get("username") or ""


def has_full_access(user_data: dict) -> bool:
    """Админы не зависят ни от техработ, ни от подписки."""
    return is_maintenance_whitelisted(resolve_username(user_data)) or is_admin_username(resolve_username(user_data))


async def check_subscription(user_id: int, force: bool = False):
    """True/False. None — проверить не удалось (тогда не блокируем игроков, но пишем в лог)."""
    global _sub_error_logged_at
    if not REQUIRED_CHANNEL:
        return True
    cached = _sub_cache.get(user_id)
    if cached and not force:
        ttl = _SUB_TTL_OK if cached[0] else _SUB_TTL_NO
        if time.time() - cached[1] < ttl:
            return cached[0]
    try:
        url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getChatMember"
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"chat_id": REQUIRED_CHANNEL, "user_id": user_id}) as resp:
                data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "getChatMember failed"))
        result = data["result"]
        status = result.get("status")
        is_member = status in ("creator", "administrator", "member") or (status == "restricted" and result.get("is_member"))
        _sub_cache[user_id] = (bool(is_member), time.time())
        return bool(is_member)
    except Exception as e:
        if time.time() - _sub_error_logged_at > 60:
            _sub_error_logged_at = time.time()
            logger.error("Проверка подписки не работает (%s). Бот должен быть админом канала %s.", e, REQUIRED_CHANNEL)
        return None


async def subscription_state(user_data: dict, force: bool = False) -> dict:
    """Что показать мини-аппу про подписку для этого пользователя."""
    if not REQUIRED_CHANNEL or has_full_access(user_data):
        return {"required": False, "subscribed": True, "url": CHANNEL_SUB_URL}
    member = await check_subscription(int(user_data["id"]), force=force)
    return {"required": True, "subscribed": member is not False, "url": CHANNEL_SUB_URL}


@web.middleware
async def blocked_user_middleware(request, handler):
    """Единый серверный замок для /api/*: чёрный список -> техработы -> подписка.
    Даже если кто-то обойдёт экран в мини-аппе и дёрнет API напрямую — сервер не пустит."""
    path = request.path
    if path.startswith("/api/") and request.method != "OPTIONS":
        user_data = get_authenticated_user(request, allow_blocked=True)
        if user_data:
            uid = int(user_data["id"])
            if path != "/api/user" and db.is_user_blocked(uid):
                return web.json_response(
                    {"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"},
                    status=403, headers=cors_headers(),
                )
            if path not in OPEN_PATHS:
                full = has_full_access(user_data)
                if db.is_maintenance() and not full:
                    return web.json_response(
                        {"error": "MAINTENANCE", "message": "Технические работы"},
                        status=503, headers=cors_headers(),
                    )
                if REQUIRED_CHANNEL and not full:
                    if (await check_subscription(uid)) is False:
                        return web.json_response(
                            {"error": "NOT_SUBSCRIBED", "message": "Подпишитесь на канал", "url": CHANNEL_SUB_URL},
                            status=403, headers=cors_headers(),
                        )
    return await handler(request)


def cors_headers():
    origin = os.getenv("WEBAPP_ORIGIN", "*")
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Init-Data, X-Launch-Token",
    }


async def handle_options(request):
    return web.Response(headers=cors_headers())


async def get_user(request):
    user_data = get_authenticated_user(request, allow_blocked=True)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    user = db.get_user(user_data["id"])
    if not user:
        db.add_user(user_data["id"], user_data.get("username", ""), user_data.get("first_name", ""))
        user = db.get_user(user_data["id"])
    elif user_data.get("username") and user_data["username"] != user.get("username"):
        db.add_user(user_data["id"], user_data.get("username", ""), user_data.get("first_name", "") or user.get("first_name", ""))
    full = has_full_access(user_data)
    maintenance_on = db.is_maintenance()
    sub = await subscription_state(user_data)
    return web.json_response({
        "user_id": user["user_id"],
        "username": user["username"],
        "first_name": user["first_name"],
        "balance": user["balance"],
        "is_blocked": bool(user.get("is_blocked", False)),
        # доступ
        "maintenance": bool(maintenance_on and not full),
        "maintenance_bypass": bool(maintenance_on and full),
        "is_admin": bool(full),
        "sub_required": sub["required"],
        "subscribed": sub["subscribed"],
        "sub_url": sub["url"],
    }, headers=cors_headers())


async def check_sub_endpoint(request):
    """Кнопка «Я подписался» — проверка без кэша."""
    user_data = get_authenticated_user(request, allow_blocked=True)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    sub = await subscription_state(user_data, force=True)
    return web.json_response({"subscribed": sub["subscribed"], "required": sub["required"], "url": sub["url"]}, headers=cors_headers())


def cleanup_expired_crash_session(user_id: int):
    """Release a Crash session only after its server-side crash point is reached."""
    session = game_sessions.get(user_id)
    if not session or not session.get("active") or session.get("game") != "crash":
        return False
    elapsed = max(0.0, time.time() - float(session.get("started_at_wall", time.time())))
    crash_point = float(session["crash_point"])
    crash_elapsed = crash_elapsed_for_multiplier(crash_point)
    if elapsed + 0.15 >= crash_elapsed:
        db.add_game_history(user_id, "crash", session["bet"], f"lose x{crash_point:.2f}", 0)
        game_sessions.pop(user_id, None)
        return True
    return False


def get_active_game(user_id: int):
    """Return the user's active game, automatically finalizing an expired Crash."""
    cleanup_expired_crash_session(user_id)
    session = game_sessions.get(user_id)
    if session and session.get("active"):
        return session
    return None


async def game_status(request):
    body = await request.json() if request.can_read_body else {}
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())
    session = get_active_game(user_data["id"])
    if not session:
        return web.json_response({"active": False, "game": None}, headers=cors_headers())
    response = {
        "active": True,
        "game": session.get("game"),
        "bet": session.get("bet", 0),
    }
    if session.get("game") == "minecraft":
        response.update({
            "clicks": int(session.get("clicks", 0)),
            "current_multiplier": float(session.get("current_multiplier", 1.0)),
            "revealed": list(session.get("revealed", [])),
        })
    elif session.get("game") == "mines":
        response.update({
            "mines_count": int(session.get("mines_count", 3)),
            "revealed": list(session.get("revealed", [])),
            "multiplier": float(session.get("multiplier", 1.0)),
        })
    elif session.get("game") == "crash":
        elapsed = max(0.0, time.time() - float(session.get("started_at_wall", time.time())))
        crash_point = float(session.get("crash_point", 1.0))
        current = crash_multiplier_at_elapsed(elapsed)
        response.update({
            "current_multiplier": current,
            "crash_point": crash_point,
            "started_at_ms": int(float(session.get("started_at_wall", time.time())) * 1000),
        })
    return web.json_response(response, headers=cors_headers())


async def play_slots(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())
    user_id = user_data["id"]
    bet = int(body.get("bet", 100))
    if bet < 100:
        return web.json_response({"error": "Min bet is 100"}, status=400, headers=cors_headers())
    if not db.deduct_balance(user_id, bet):
        return web.json_response({"error": "Insufficient balance"}, status=400, headers=cors_headers())

    symbols = ["🍋", "🍊", "🍇", "🍒", "⭐", "7️⃣"]
    win_chance = 0.15  # 15% fixed, not secretly changed
    # A 777 win returns exactly 2x the stake (stake + same amount as profit).
    is_win = random.random() < win_chance
    if is_win:
        result = ["7️⃣", "7️⃣", "7️⃣"]
        win_amount = bet * 2
        db.add_balance(user_id, win_amount)
        db.add_game_history(user_id, "slots", bet, "777", win_amount)
    else:
        result = []
        for _ in range(3):
            s = random.choice([s for s in symbols if s != "7️⃣"])
            result.append(s)
        while result == ["7️⃣", "7️⃣", "7️⃣"]:
            result = [random.choice([s for s in symbols if s != "7️⃣"]) for _ in range(3)]
        win_amount = 0
        db.add_game_history(user_id, "slots", bet, "".join(result), 0)

    new_balance = db.get_balance(user_id)
    return web.json_response({
        "result": result,
        "win": is_win,
        "win_amount": win_amount,
        "total_return": win_amount,
        "profit": (win_amount - bet) if is_win else 0,
        "payout_multiplier": 2,
        "balance": new_balance,
        "server_version": "v12",
    }, headers=cors_headers())


# ============================================================
# MINES — rigged: bombs fall on clicked cell ~35% of the time
# (even if that cell is technically "safe" in the generated grid)
# ============================================================
async def play_mines(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())

    user_id = user_data["id"]
    action = body.get("action")

    if action == "start":
        cleanup_expired_crash_session(user_id)
        if user_id in game_sessions and game_sessions[user_id].get("active"):
            return web.json_response({"error": "Finish the active game first"}, status=400, headers=cors_headers())
        try:
            bet = int(body.get("bet", 100))
            mines_count = int(body.get("mines", 3))
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid bet or mines count"}, status=400, headers=cors_headers())
        if bet < 500:
            return web.json_response({"error": "Min bet is 500"}, status=400, headers=cors_headers())
        if mines_count not in [3, 5, 10]:
            return web.json_response({"error": "Invalid mines count"}, status=400, headers=cors_headers())
        if not db.deduct_balance(user_id, bet):
            return web.json_response({"error": "Insufficient balance"}, status=400, headers=cors_headers())

        # Generate mine positions (will be overridden with rigged logic on reveal)
        mine_positions = random.sample(range(25), mines_count)
        grid = [False] * 25
        for pos in mine_positions:
            grid[pos] = True

        session = {
            "game": "mines",
            "user_id": user_id,
            "bet": bet,
            "mines_count": mines_count,
            "grid": grid,
            "mine_positions": mine_positions,
            "revealed": [],
            "multiplier": 1.0,
            "active": True,
        }
        game_sessions[user_id] = session
        return web.json_response({
            "status": "started", "bet": bet, "mines": mines_count,
            "balance": db.get_balance(user_id),
        }, headers=cors_headers())

    elif action == "reveal":
        try:
            cell = int(body.get("cell"))
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid cell"}, status=400, headers=cors_headers())
        if cell < 0 or cell >= 25:
            return web.json_response({"error": "Invalid cell"}, status=400, headers=cors_headers())
        session = game_sessions.get(user_id)
        if not session or not session["active"]:
            return web.json_response({"error": "No active game"}, status=400, headers=cors_headers())
        if cell in session["revealed"]:
            return web.json_response({"error": "Already revealed"}, status=400, headers=cors_headers())

        # The minefield is fixed for the whole round. Do not change the grid
        # after the player has started clicking cells.
        is_mine = session["grid"][cell]

        session["revealed"].append(cell)

        if is_mine:
            session["active"] = False
            db.add_game_history(user_id, "mines", session["bet"], "lose", 0)
            del game_sessions[user_id]
            mine_pos = [i for i, v in enumerate(session["grid"]) if v]
            return web.json_response({
                "hit_mine": True,
                "mine_positions": mine_pos,
                "balance": db.get_balance(user_id),
            }, headers=cors_headers())
        else:
            revealed_safe = len(session["revealed"])
            multiplier = calculate_multiplier(session["mines_count"], revealed_safe)
            session["multiplier"] = multiplier
            db.save_game_session(user_id, session)
            return web.json_response({
                "hit_mine": False, "multiplier": multiplier,
                "revealed": session["revealed"], "balance": db.get_balance(user_id),
            }, headers=cors_headers())

    elif action == "cashout":
        session = game_sessions.get(user_id)
        if not session or not session["active"]:
            return web.json_response({"error": "No active game"}, status=400, headers=cors_headers())
        if not session["revealed"]:
            db.add_balance(user_id, session["bet"])
            del game_sessions[user_id]
            return web.json_response({"error": "No cells revealed"}, status=400, headers=cors_headers())
        win_amount = int(session["bet"] * session["multiplier"])
        db.add_balance(user_id, win_amount)
        db.add_game_history(user_id, "mines", session["bet"], f"win x{session['multiplier']:.2f}", win_amount)
        session["active"] = False
        del game_sessions[user_id]
        return web.json_response({
            "win_amount": win_amount, "multiplier": session["multiplier"],
            "balance": db.get_balance(user_id),
        }, headers=cors_headers())

    return web.json_response({"error": "Invalid action"}, status=400, headers=cors_headers())


def calculate_multiplier(mines: int, revealed: int) -> float:
    safe = 25 - mines
    mult = 1.0
    for i in range(revealed):
        mult *= (safe - i) / (25 - i)
    base = (1.0 / mult * 0.95) if mult > 0 else 1.0
    # 3 mines was growing too aggressively. Keep the early values close to 1x
    # and cap the round at 3x for this difficulty. Other mine counts keep the
    # existing curve.
    if mines == 3:
        return min(round(1.0 + (base - 1.0) * 0.55, 2), 3.0)
    return round(base, 2)


# ============================================================
# UPGRADE — fixed win chances per multiplier
# ============================================================
UPGRADE_CHANCES = {
    1.5: 50,
    2.0: 35,
    3.0: 25,
    5.0: 15,
    10.0: 10,
}

async def get_catalog(request):
    """Кейсы, шансы, подарки и шансы Upgrade — мини-апп рисует всё из этих данных,
    поэтому цифры на экране всегда совпадают с тем, что реально считает сервер."""
    data = cases_config.public_catalog()
    data["upgrade_chances"] = {str(k): v for k, v in UPGRADE_CHANCES.items()}
    return web.json_response(data, headers=cors_headers())


async def play_upgrade(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())

    user_id = user_data["id"]
    bet = int(body.get("bet", 100))
    mult = float(body.get("mult", 2.0))

    if mult not in UPGRADE_CHANCES:
        return web.json_response({"error": "Invalid multiplier"}, status=400, headers=cors_headers())
    if bet < 100:
        return web.json_response({"error": "Min bet is 100"}, status=400, headers=cors_headers())
    if not db.deduct_balance(user_id, bet):
        return web.json_response({"error": "Insufficient balance"}, status=400, headers=cors_headers())

    win_chance = UPGRADE_CHANCES[mult]  # percent
    # Roll 0-99.99
    roll = round(random.uniform(0, 100), 2)
    is_win = roll < win_chance

    if is_win:
        win_amount = int(bet * mult)
        db.add_balance(user_id, win_amount)
        db.add_game_history(user_id, "upgrade", bet, f"win x{mult}", win_amount)
        profit = win_amount - bet
    else:
        win_amount = 0
        profit = 0
        db.add_game_history(user_id, "upgrade", bet, f"lose x{mult}", 0)

    return web.json_response({
        "win": is_win,
        "roll": roll,
        "win_amount": profit,   # profit (not total), so frontend shows +X
        "balance": db.get_balance(user_id),
    }, headers=cors_headers())



async def play_ufc(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())

    user_id = user_data["id"]
    bet = int(body.get("bet", 100))
    if bet < 100:
        return web.json_response({"error": "Min bet is 100"}, status=400, headers=cors_headers())
    if bet > 5000:
        return web.json_response({"error": "Max bet is 5000"}, status=400, headers=cors_headers())

    # Pick the winning side randomly at the start of every fight.
    # The player's result is determined by whether their selected side
    # matches the randomly chosen winner. This prevents one side (e.g. blue)
    # from being the default winner.
    selected_side = str(body.get("side", "")).lower()
    if selected_side not in ("red", "blue"):
        return web.json_response({"error": "Invalid side"}, status=400, headers=cors_headers())
    if not db.deduct_balance(user_id, bet):
        return web.json_response({"error": "Insufficient balance"}, status=400, headers=cors_headers())

    winner_side = random.choice(("red", "blue"))
    is_win = selected_side == winner_side

    if is_win:
        win_amount = bet * 2
        db.add_balance(user_id, win_amount)
        db.add_game_history(user_id, "ufc", bet, "win", win_amount)
    else:
        win_amount = 0
        db.add_game_history(user_id, "ufc", bet, "lose", 0)

    return web.json_response({
        "win": is_win,
        "winner_side": winner_side,
        "win_amount": win_amount,
        "balance": db.get_balance(user_id),
    }, headers=cors_headers())


async def activate_promo(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())
    code = body.get("code", "").strip().upper()
    result = db.activate_promo(user_data["id"], code)
    if result["success"]:
        return web.json_response(result, headers=cors_headers())
    errors = {
        "not_found": "Промокод не найден",
        "expired": "Промокод больше не активен",
        "already_used": "Вы уже использовали этот промокод",
    }
    return web.json_response(
        {"error": errors.get(result["error"], "Ошибка")},
        status=400, headers=cors_headers()
    )


async def get_history(request):
    user_data = get_authenticated_user(request)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())
    history = db.get_game_history(user_data["id"])
    return web.json_response({"history": history}, headers=cors_headers())


async def get_top(request):
    top = db.get_top_deposits(10)
    return web.json_response({"top": top}, headers=cors_headers())


class PersistentGameSessions:
    """Neon-backed session store. Prevents active games from disappearing after a restart."""
    def get(self, user_id, default=None):
        value = db.get_game_session(int(user_id))
        return value if value is not None else default

    def __contains__(self, user_id):
        return db.get_game_session(int(user_id)) is not None

    def __getitem__(self, user_id):
        value = db.get_game_session(int(user_id))
        if value is None:
            raise KeyError(user_id)
        return value

    def __setitem__(self, user_id, session):
        db.save_game_session(int(user_id), session)

    def pop(self, user_id, default=None):
        value = db.get_game_session(int(user_id))
        if value is not None:
            db.delete_game_session(int(user_id))
            return value
        return default

    def __delitem__(self, user_id):
        if db.get_game_session(int(user_id)) is None:
            raise KeyError(user_id)
        db.delete_game_session(int(user_id))

game_sessions = PersistentGameSessions()
withdrawal_requests = {}  # Для отслеживания активных запросов


# ============================================================
# WITHDRAWAL — Вывод баланса
# ============================================================
from datetime import datetime, timedelta

async def request_withdrawal(request):
    """Создать заявку на вывод. Админский unlock полностью снимает условия депозита/ожидания."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())

    user_id = int(user_data["id"])
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0

    if amount < 1000:
        return web.json_response({"error": "Минимальная сумма вывода — 1000 ⭐"}, status=400, headers=cors_headers())

    deposit_info = db.get_user_deposits(user_id)
    unlocked = bool(deposit_info and deposit_info.get("can_withdraw_unlock"))

    # Сначала проверяем unlock: разблокированный администратором игрок
    # не обязан ни вносить 1000★, ни ждать час.
    if not unlocked:
        if not deposit_info or int(deposit_info.get("total_deposited") or 0) < 1000:
            return web.json_response({
                "error": "Для вывода нужно пополнить баланс минимум на 1000 ⭐"
            }, status=400, headers=cors_headers())

        first_deposit_at = deposit_info.get("first_deposit_at")
        if not first_deposit_at:
            return web.json_response({
                "error": "Не найдена дата первого депозита. Обратитесь к администратору."
            }, status=400, headers=cors_headers())

        # Сравнение выполняется в самой БД — без проблем с timezone-aware/naive datetime.
        try:
            with db.get_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - first_deposit_at)) FROM user_deposits WHERE user_id=%s;",
                        (user_id,)
                    )
                    row = cursor.fetchone()
                    elapsed_seconds = float(row[0] or 0) if row else 0.0
        except Exception as e:
            print(f"❌ Ошибка проверки времени депозита: {e}")
            return web.json_response({"error": "Не удалось проверить время депозита"}, status=500, headers=cors_headers())

        if elapsed_seconds < 3600:
            minutes_left = max(1, int((3600 - elapsed_seconds + 59) // 60))
            return web.json_response({
                "error": f"Нужно подождать ещё {minutes_left} мин. после депозита 1000 ⭐"
            }, status=400, headers=cors_headers())

    # Списываем только после всех проверок, чтобы не возвращать баланс при ошибке.
    if not db.deduct_balance(user_id, amount):
        return web.json_response({"error": "Недостаточно звёзд"}, status=400, headers=cors_headers())

    request_id = db.create_withdrawal_request(user_id, amount)
    if not request_id:
        db.add_balance(user_id, amount)
        return web.json_response({"error": "Не удалось создать заявку на вывод"}, status=500, headers=cors_headers())

    withdrawal_requests[request_id] = {
        "user_id": user_id,
        "amount": amount,
        "created_at": datetime.now(),
    }
    return web.json_response({
        "status": "pending",
        "withdrawal_id": request_id,
        "balance": db.get_balance(user_id),
        "unlocked": unlocked,
    }, headers=cors_headers())


# ============================================================
# КЕЙСЫ / ПРОФИЛЬ / NFT-ПОДАРКИ
# ============================================================
def _blocked_response():
    return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())


def _gift_payload(row: dict) -> dict:
    g = cases_config.GIFTS.get(row["gift_key"], {})
    return {
        "id": row["id"], "key": row["gift_key"], "name": g.get("name", row["gift_key"]),
        "price": g.get("price", 0), "art": g.get("art", "pack"), "tone": g.get("tone", "#888"),
        "image": g.get("image", ""), "status": row["status"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


async def open_case(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return _blocked_response()

    case = cases_config.CASES_BY_ID.get(str(body.get("case_id", "")))
    if not case:
        return web.json_response({"error": "Такого кейса нет"}, status=400, headers=cors_headers())

    user_id = int(user_data["id"])
    kind, value = cases_config.roll_case(case)          # результат решает СЕРВЕР
    worth = cases_config.drop_value((kind, value, 0))
    if kind == "stars":
        label = f"{int(value)} ⭐"
    else:
        label = f"{cases_config.GIFTS[value]['name']} (NFT)"

    res = db.open_case(user_id, case["id"], case["name"], int(case["price"]), kind, value, label, worth)
    if res is None:
        return web.json_response({"error": "Недостаточно звёзд"}, status=400, headers=cors_headers())

    prize = {"kind": kind, "value": value, "worth": worth, "label": label}
    if kind == "gift":
        g = cases_config.GIFTS[value]
        prize.update({"name": g["name"], "art": g["art"], "tone": g["tone"], "image": g.get("image", ""),
                      "price": g["price"], "gift_id": res["gift_id"]})
    return web.json_response({"prize": prize, "balance": res["balance"]}, headers=cors_headers())


async def get_profile(request):
    user_data = get_authenticated_user(request)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return _blocked_response()
    uid = int(user_data["id"])
    user = db.get_user(uid) or {}
    gifts = [_gift_payload(r) for r in db.get_user_gifts(uid)]
    return web.json_response({
        "user_id": uid, "username": user.get("username", ""), "first_name": user.get("first_name", ""),
        "balance": user.get("balance", 0), "gifts": gifts, "stats": db.get_profile_stats(uid),
    }, headers=cors_headers())


async def sell_gift(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return _blocked_response()
    try:
        gift_id = int(body.get("gift_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Некорректный подарок"}, status=400, headers=cors_headers())
    prices = {k: v["price"] for k, v in cases_config.GIFTS.items()}
    res = db.sell_gift(int(user_data["id"]), gift_id, prices)
    if not res:
        return web.json_response({"error": "Подарок не найден или уже продан"}, status=400, headers=cors_headers())
    return web.json_response({
        "sold": True, "price": res["price"], "balance": res["balance"],
        "name": cases_config.GIFTS[res["gift_key"]]["name"],
    }, headers=cors_headers())


def _withdraw_eligibility_error(user_id: int):
    """Те же условия, что и для вывода звёзд (депозит от 1000★ + 1 час), если админ не разблокировал вывод."""
    info = db.get_user_deposits(user_id)
    if info and info.get("can_withdraw_unlock"):
        return None
    if not info or int(info.get("total_deposited") or 0) < 1000:
        return "Для вывода нужно пополнить баланс минимум на 1000 ⭐"
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - first_deposit_at)) FROM user_deposits WHERE user_id=%s;",
                    (user_id,),
                )
                row = cursor.fetchone()
                elapsed = float(row[0] or 0) if row else 0.0
    except Exception as e:
        print(f"❌ Ошибка проверки времени депозита: {e}")
        return "Не удалось проверить условия вывода"
    if elapsed < 3600:
        return f"Нужно подождать ещё {max(1, int((3600 - elapsed + 59) // 60))} мин. после депозита"
    return None


async def withdraw_gift(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return _blocked_response()
    try:
        gift_id = int(body.get("gift_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Некорректный подарок"}, status=400, headers=cors_headers())
    uid = int(user_data["id"])
    err = _withdraw_eligibility_error(uid)
    if err:
        return web.json_response({"error": err}, status=400, headers=cors_headers())
    key = db.request_gift_withdrawal(uid, gift_id)
    if not key:
        return web.json_response({"error": "Подарок не найден или уже в обработке"}, status=400, headers=cors_headers())
    return web.json_response({"status": "pending", "name": cases_config.GIFTS.get(key, {}).get("name", key)}, headers=cors_headers())


# ============================================================
# MINECRAFT — Lucky blocks game with progressive multipliers
# ============================================================
MINECRAFT_BLOCKS = 5

MINECRAFT_CHANCES = {
    1.5: 40,
    2.0: 30,
    3.0: 25,
    4.0: 20,
    5.0: 15,
}
MINECRAFT_MULTIPLIERS = [1.5, 2.0, 3.0, 4.0, 5.0]

async def play_minecraft(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())

    user_id = user_data["id"]
    action = body.get("action")

    if action == "start":
        cleanup_expired_crash_session(user_id)
        if user_id in game_sessions and game_sessions[user_id].get("active"):
            return web.json_response({"error": "Finish the active game first"}, status=400, headers=cors_headers())
        try:
            bet = int(body.get("bet", 100))
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid bet"}, status=400, headers=cors_headers())
        if bet < 100 or bet > 10000:
            return web.json_response({"error": "Bet must be 100-10000"}, status=400, headers=cors_headers())
        if not db.deduct_balance(user_id, bet):
            return web.json_response({"error": "Insufficient balance"}, status=400, headers=cors_headers())

        game_sessions[user_id] = {
            "game": "minecraft",
            "user_id": user_id,
            "bet": bet,
            "current_multiplier": 1.0,
            "current_win": 0,
            "clicks": 0,
            "revealed": [],
            "active": True,
        }
        db.save_game_session(user_id, game_sessions[user_id])
        return web.json_response({
            "status": "started", "bet": bet, "multiplier": 1.0,
            "balance": db.get_balance(user_id),
        }, headers=cors_headers())

    elif action == "mine":
        session = game_sessions.get(user_id)
        if not session or not session.get("active") or session.get("game") != "minecraft":
            return web.json_response({"error": "No active game"}, status=400, headers=cors_headers())
        try:
            cell = int(body.get("cell"))
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid cell"}, status=400, headers=cors_headers())
        if cell < 0 or cell >= MINECRAFT_BLOCKS:
            return web.json_response({"error": "Invalid cell"}, status=400, headers=cors_headers())
        if cell in session.setdefault("revealed", []):
            return web.json_response({"error": "Already revealed"}, status=400, headers=cors_headers())
        session["revealed"].append(cell)

        next_idx = session["clicks"]
        target_mult = MINECRAFT_MULTIPLIERS[next_idx] if next_idx < len(MINECRAFT_MULTIPLIERS) else MINECRAFT_MULTIPLIERS[-1]
        win_chance = MINECRAFT_CHANCES[target_mult]
        is_win = random.random() * 100 < win_chance

        if not is_win:
            session["active"] = False
            db.add_game_history(user_id, "minecraft", session["bet"], f"lose at x{target_mult}", 0)
            del game_sessions[user_id]
            return web.json_response({
                "hit_bomb": True,
                "multiplier": target_mult,
                "balance": db.get_balance(user_id),
            }, headers=cors_headers())

        session["clicks"] += 1
        session["current_multiplier"] = target_mult
        session["current_win"] = int(session["bet"] * target_mult)
        db.save_game_session(user_id, session)

        if session["clicks"] >= len(MINECRAFT_MULTIPLIERS):
            session["active"] = False
            win_amount = session["current_win"]
            db.add_balance(user_id, win_amount)
            db.add_game_history(user_id, "minecraft", session["bet"], f"win x{target_mult}", win_amount)
            del game_sessions[user_id]
            return web.json_response({
                "hit_bomb": False, "max_reached": True,
                "multiplier": target_mult, "win_amount": win_amount,
                "balance": db.get_balance(user_id),
            }, headers=cors_headers())

        return web.json_response({
            "hit_bomb": False, "multiplier": target_mult,
            "next_multiplier": MINECRAFT_MULTIPLIERS[session["clicks"]],
            "win_amount": session["current_win"],
            "balance": db.get_balance(user_id),
        }, headers=cors_headers())

    elif action == "cashout":
        session = game_sessions.get(user_id)
        if not session or session.get("game") != "minecraft":
            return web.json_response({"error": "No active game"}, status=400, headers=cors_headers())

        # Idempotent cashout: if Telegram/Render delivered the request but the
        # response was lost, the next tap returns the exact same payout instead
        # of getting stuck on "Забираем..." or paying twice.
        if not session.get("active") and session.get("cashout_result"):
            result = dict(session["cashout_result"])
            result["idempotent"] = True
            result["balance"] = db.get_balance(user_id)
            return web.json_response(result, headers=cors_headers())

        if not session.get("active"):
            return web.json_response({"error": "No active game"}, status=400, headers=cors_headers())
        if session["clicks"] <= 0:
            return web.json_response({"error": "Open at least one block first"}, status=400, headers=cors_headers())
        win_amount = int(session["current_win"])
        multiplier = float(session["current_multiplier"])
        db.add_balance(user_id, win_amount)
        db.add_game_history(user_id, "minecraft", session["bet"], f"cashout x{multiplier}", win_amount)
        result = {
            "win_amount": win_amount,
            "multiplier": multiplier,
            "balance": db.get_balance(user_id),
        }
        session["active"] = False
        session["cashout_result"] = result
        # Keep the receipt in Neon for safe retry after a lost HTTP response.
        db.save_game_session(user_id, session)
        return web.json_response(result, headers=cors_headers())

    return web.json_response({"error": "Invalid action"}, status=400, headers=cors_headers())


# ============================================================
# CRASH — Rocket game with multiplier
# ============================================================
# Счётчик ставок до следующего 1.00x краша (per user)
# Значение = через сколько ставок будет принудительный 1.00x
CRASH_MAX_MULTIPLIER = 10.0
# Crash progression: +0.10x every 1 second until 2.00x, then +0.10x every 0.5s.
CRASH_PRE_2_SPEED = 0.10
CRASH_POST_2_SPEED = 0.20
CRASH_2X_SECONDS = 10.0
# Per-user cooldown to prevent rapid-fire dupe bets (seconds)
CRASH_MIN_INTERVAL = 2.0
crash_last_play: dict = {}  # user_id -> wall time of last play_crash call

def crash_multiplier_at_elapsed(elapsed: float) -> float:
    elapsed = max(0.0, float(elapsed))
    if elapsed <= CRASH_2X_SECONDS:
        return min(2.0, 1.0 + elapsed * CRASH_PRE_2_SPEED)
    return min(CRASH_MAX_MULTIPLIER, 2.0 + (elapsed - CRASH_2X_SECONDS) * CRASH_POST_2_SPEED)

def crash_elapsed_for_multiplier(multiplier: float) -> float:
    multiplier = max(1.0, min(CRASH_MAX_MULTIPLIER, float(multiplier)))
    if multiplier <= 2.0:
        return (multiplier - 1.0) / CRASH_PRE_2_SPEED
    return CRASH_2X_SECONDS + (multiplier - 2.0) / CRASH_POST_2_SPEED

def generate_crash_point(user_id: int = 0, bet: int = 0) -> float:
    """
    Генерирует краш-поинт через криптографически стойкий RNG.
    НЕ использует предсказуемый счётчик — каждый раунд независим.
    Распределение:
      15% — мгновенный краш 1.00x
      35% — 1.10x–1.60x
      25% — 1.60x–2.20x
      13% — 2.20x–3.50x
       7% — 3.50x–6.00x
       5% — 6.00x–10.00x
    Использует secrets для непредсказуемости.
    """
    import secrets
    # Получаем 4 байта случайности от OS
    raw = secrets.token_bytes(4)
    roll = int.from_bytes(raw, "big") / 0xFFFFFFFF  # float [0, 1)

    if roll < 0.15:
        return 1.00                                        # 15% — мгновенный краш
    if roll < 0.50:
        return round(1.10 + (roll - 0.15) / 0.35 * 0.50, 2)  # 1.10–1.60
    if roll < 0.75:
        return round(1.60 + (roll - 0.50) / 0.25 * 0.60, 2)  # 1.60–2.20
    if roll < 0.88:
        return round(2.20 + (roll - 0.75) / 0.13 * 1.30, 2)  # 2.20–3.50
    if roll < 0.95:
        return round(3.50 + (roll - 0.88) / 0.07 * 2.50, 2)  # 3.50–6.00
    return round(6.00 + (roll - 0.95) / 0.05 * 4.00, 2)       # 6.00–10.00

async def play_crash(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())

    user_id = user_data["id"]
    try:
        bet = int(body.get("bet", 100))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid bet"}, status=400, headers=cors_headers())
    if bet < 100 or bet > 50000:
        return web.json_response({"error": "Bet must be 100-50000"}, status=400, headers=cors_headers())

    # --- DUPE FIX: rate-limit — нельзя делать ставку быстрее чем CRASH_MIN_INTERVAL ---
    now = time.time()
    last = crash_last_play.get(user_id, 0.0)
    if now - last < CRASH_MIN_INTERVAL:
        return web.json_response(
            {"error": "Too fast", "retry_after": round(CRASH_MIN_INTERVAL - (now - last), 2)},
            status=429, headers=cors_headers()
        )
    crash_last_play[user_id] = now

    cleanup_expired_crash_session(user_id)
    if user_id in game_sessions and game_sessions[user_id].get("active"):
        return web.json_response({"error": "Finish the active game first"}, status=400, headers=cors_headers())
    if not db.deduct_balance(user_id, bet):
        return web.json_response({"error": "Insufficient balance"}, status=400, headers=cors_headers())

    crash_point = generate_crash_point(user_id, bet)
    wall_now = time.time()
    game_sessions[user_id] = {
        "game": "crash",
        "user_id": user_id,
        "bet": bet,
        "crash_point": crash_point,
        "started_at": time.monotonic(),
        "started_at_wall": wall_now,
        "active": True,
    }
    # crash_point отдаём клиенту только для анимации.
    # Сервер всё равно проверяет время на cashout — клиент не может подделать результат.
    crash_elapsed = crash_elapsed_for_multiplier(crash_point)
    return web.json_response({
        "status": "started",
        "multiplier": crash_point,
        "crash_elapsed_ms": int(crash_elapsed * 1000),
        "balance": db.get_balance(user_id),
        "max_multiplier": CRASH_MAX_MULTIPLIER,
        "started_at_ms": int(wall_now * 1000),
    }, headers=cors_headers())

async def crash_cashout(request):
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())
    user_id = user_data["id"]
    session = game_sessions.get(user_id)
    if not session or not session.get("active") or session.get("game") != "crash":
        return web.json_response({"error": "No active crash game"}, status=400, headers=cors_headers())

    elapsed = max(0.0, time.time() - float(session.get("started_at_wall", time.time())))
    server_mult = crash_multiplier_at_elapsed(elapsed)
    crash_point = session["crash_point"]
    if server_mult >= crash_point:
        session["active"] = False
        db.add_game_history(user_id, "crash", session["bet"], f"lose x{crash_point:.2f}", 0)
        del game_sessions[user_id]
        return web.json_response({
            "error": "Crash already happened",
            "crashed": True,
            "multiplier": crash_point,
            "balance": db.get_balance(user_id),
        }, status=400, headers=cors_headers())

    multiplier = round(min(server_mult, crash_point - 0.01), 2)
    multiplier = max(1.00, multiplier)
    win_amount = int(session["bet"] * multiplier)
    db.add_balance(user_id, win_amount)
    db.add_game_history(user_id, "crash", session["bet"], f"cashout x{multiplier:.2f}", win_amount)
    session["active"] = False
    del game_sessions[user_id]
    return web.json_response({
        "win": True,
        "win_amount": win_amount,
        "multiplier": multiplier,
        "balance": db.get_balance(user_id),
    }, headers=cors_headers())


async def crash_resolve(request):
    """Finalize a Crash round as a loss once its server-side crash point has elapsed."""
    body = await request.json()
    user_data = get_authenticated_user(request, body)
    if not user_data:
        return web.json_response({"error": "Unauthorized"}, status=401, headers=cors_headers())
    if user_data.get("_blocked"):
        return web.json_response({"error": "USER_BLOCKED", "message": "Пользователь заблокирован администратором"}, status=403, headers=cors_headers())
    user_id = user_data["id"]
    session = game_sessions.get(user_id)
    if not session or not session.get("active") or session.get("game") != "crash":
        return web.json_response({"error": "No active crash game"}, status=400, headers=cors_headers())
    elapsed = max(0.0, time.time() - float(session.get("started_at_wall", time.time())))
    crash_point = float(session["crash_point"])
    crash_elapsed = crash_elapsed_for_multiplier(crash_point)
    if elapsed + 0.12 < crash_elapsed:
        return web.json_response({"error": "Crash point not reached yet"}, status=409, headers=cors_headers())
    bet = session["bet"]
    db.add_game_history(user_id, "crash", bet, f"lose x{crash_point:.2f}", 0)
    del game_sessions[user_id]
    return web.json_response({
        "crashed": True,
        "multiplier": crash_point,
        "balance": db.get_balance(user_id),
    }, headers=cors_headers())


def create_app():
    import os
    webapp_path = os.path.join(os.path.dirname(__file__), "..", "webapp")
    webapp_path = os.path.abspath(webapp_path)
    app = web.Application(middlewares=[blocked_user_middleware])
    async def _index(_request):
        return web.FileResponse(
            os.path.join(webapp_path, "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}
        )

    app.router.add_route("GET", "/", _index)

    async def _health(_request):
        return web.json_response({"ok": True, "service": "1win-bot"})

    app.router.add_get("/health", _health)
    app.router.add_get("/api/user", get_user)
    app.router.add_post("/api/subscription/check", check_sub_endpoint)
    app.router.add_get("/api/catalog", get_catalog)
    app.router.add_post("/api/cases/open", open_case)
    app.router.add_get("/api/profile", get_profile)
    app.router.add_post("/api/gifts/sell", sell_gift)
    app.router.add_post("/api/gifts/withdraw", withdraw_gift)
    app.router.add_post("/api/game/status", game_status)
    app.router.add_post("/api/slots/play", play_slots)
    app.router.add_post("/api/mines/play", play_mines)
    app.router.add_post("/api/upgrade/play", play_upgrade)
    app.router.add_post("/api/minecraft/play", play_minecraft)
    app.router.add_post("/api/crash/play", play_crash)
    app.router.add_post("/api/crash/cashout", crash_cashout)
    app.router.add_post("/api/crash/resolve", crash_resolve)
    app.router.add_post("/api/ufc/play", play_ufc)
    app.router.add_post("/api/promo/activate", activate_promo)
    app.router.add_post("/api/withdrawal/request", request_withdrawal)
    app.router.add_get("/api/history", get_history)
    app.router.add_get("/api/top", get_top)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_options)
    app.router.add_static("/static", path=os.path.join(webapp_path, "static"), name="static")
    return app


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
    web.run_app(app, host=host, port=port)

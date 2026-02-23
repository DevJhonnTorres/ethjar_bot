import os
import re
import json
import asyncio
import subprocess
import logging
import time
import threading
from typing import Optional
from urllib.parse import quote

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENCLAW_API = os.getenv("OPENCLAW_API", "http://localhost:8000/chat")
USE_OPENCLAW_CLI = os.getenv("USE_OPENCLAW_CLI", "0") == "1"
USE_OPENCLAW_HTTP = os.getenv("USE_OPENCLAW_HTTP", "0") == "1"
POLLINATIONS_URL = os.getenv("POLLINATIONS_URL", "https://text.pollinations.ai")
POLLINATIONS_OPENAI_URL = os.getenv(
    "POLLINATIONS_OPENAI_URL", "https://text.pollinations.ai/openai"
)
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "openai")
BOT_NAME = os.getenv("BOT_NAME", "OpenClaw")
MAX_TELEGRAM_MESSAGE_LEN = 4096
COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY")
COINMARKETCAP_QUOTES_URL = os.getenv(
    "COINMARKETCAP_QUOTES_URL",
    "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
)
COINMARKETCAP_CONVERT = os.getenv("COINMARKETCAP_CONVERT", "USD").upper()
WHO_WE_ARE_URL = "https://www.ethcali.org/"
CRYPTO_SYMBOL_ALIASES = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "ripple": "XRP",
    "xrp": "XRP",
    "binance coin": "BNB",
    "bnb": "BNB",
    "dogecoin": "DOGE",
    "doge": "DOGE",
    "cardano": "ADA",
    "ada": "ADA",
    "litecoin": "LTC",
    "ltc": "LTC",
    "polkadot": "DOT",
    "dot": "DOT",
    "tron": "TRX",
    "trx": "TRX",
    "avalanche": "AVAX",
    "avax": "AVAX",
    "chainlink": "LINK",
    "link": "LINK",
    "polygon": "MATIC",
    "matic": "MATIC",
    "shiba": "SHIB",
    "shib": "SHIB",
    "usdt": "USDT",
    "usdc": "USDC",
}

logger = logging.getLogger(__name__)
CHAT_LOCKS: dict[int, asyncio.Lock] = {}
BACKEND_COOLDOWN_UNTIL: dict[str, float] = {}
BACKEND_FAIL_STREAK: dict[str, int] = {}
BACKEND_STATE_LOCK = threading.Lock()


def backend_is_available(backend_name: str) -> bool:
    now = time.time()
    with BACKEND_STATE_LOCK:
        until = BACKEND_COOLDOWN_UNTIL.get(backend_name, 0.0)
        if until <= now:
            BACKEND_COOLDOWN_UNTIL.pop(backend_name, None)
            return True
        return False


def register_backend_success(backend_name: str) -> None:
    with BACKEND_STATE_LOCK:
        BACKEND_FAIL_STREAK[backend_name] = 0
        BACKEND_COOLDOWN_UNTIL.pop(backend_name, None)


def register_backend_failure(backend_name: str, exc: Exception) -> None:
    error_text = str(exc).lower()
    now = time.time()
    with BACKEND_STATE_LOCK:
        streak = BACKEND_FAIL_STREAK.get(backend_name, 0) + 1
        BACKEND_FAIL_STREAK[backend_name] = streak

        cooldown_seconds = 0
        if (
            "connection refused" in error_text
            or "failed to establish a new connection" in error_text
            or "name or service not known" in error_text
            or "nodename nor servname provided" in error_text
        ):
            cooldown_seconds = 120
        elif (
            "429" in error_text
            or "queue full" in error_text
            or "rate limit" in error_text
            or "too many requests" in error_text
        ):
            cooldown_seconds = min(180, 20 * streak)
        elif "timeout" in error_text:
            cooldown_seconds = min(90, 15 * streak)

        if cooldown_seconds > 0:
            BACKEND_COOLDOWN_UNTIL[backend_name] = now + cooldown_seconds


def ask_openclaw_http(user_message: str) -> str:
    response = requests.post(
        OPENCLAW_API,
        json={"message": user_message},
        timeout=(2, 8),
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        text = response.text.strip()
        return text or "Sin respuesta."

    data = response.json()
    return (
        data.get("response")
        or data.get("reply")
        or data.get("text")
        or "Sin respuesta."
    )


def ask_openclaw_cli(chat_id: int, user_message: str) -> str:
    command = [
        "zsh",
        "-lic",
        f"openclaw agent --local --json --session-id tg-{chat_id} -m {json.dumps(user_message)}",
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or "openclaw error")

    payload = json.loads(process.stdout)
    payloads = payload.get("payloads", [])
    if not payloads:
        return "Sin respuesta."
    return payloads[0].get("text", "Sin respuesta.")


def ask_fallback_llm(user_message: str) -> str:
    payload = {
        "model": POLLINATIONS_MODEL,
        "messages": [
            {"role": "system", "content": "Responde en espanol, breve y util."},
            {"role": "user", "content": user_message},
        ],
    }
    for attempt in range(2):
        try:
            response = requests.post(POLLINATIONS_OPENAI_URL, json=payload, timeout=(3, 10))
            response.raise_for_status()
            data = response.json()

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(str(data["error"]))

            choices = data.get("choices", []) if isinstance(data, dict) else []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message", {})
                if isinstance(message, dict):
                    content = message.get("content", "")
                    if content and str(content).strip():
                        return str(content).strip()

            raise RuntimeError("Respuesta invalida de pollinations-openai")
        except Exception as exc:
            error_text = str(exc).lower()
            retryable = (
                "timeout" in error_text
                or "429" in error_text
                or "queue full" in error_text
                or "rate limit" in error_text
            )
            if attempt < 2 and retryable:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise


def ask_fallback_llm_legacy(user_message: str) -> str:
    # Legacy GET path fallback: keep prompt short to reduce timeouts/queue issues.
    prompt = quote(
        (
            "Responde breve en espanol. "
            f"{user_message}"
        ),
        safe="",
    )
    response = requests.get(f"{POLLINATIONS_URL}/{prompt}", timeout=(2, 10))
    response.raise_for_status()
    text = response.text.strip()
    if text.startswith("{") and '"error"' in text:
        raise RuntimeError(text)
    return text or "Sin respuesta."


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    lock = CHAT_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        CHAT_LOCKS[chat_id] = lock
    return lock


def normalize_identity(reply: str) -> str:
    if not reply:
        return ""

    text = reply.strip()
    lower = text.lower()
    if "chatgpt" in lower:
        text = re.sub("chatgpt", BOT_NAME, text, flags=re.IGNORECASE)
    if "creado por openai" in lower or "created by openai" in lower:
        text = re.sub(
            r"creado por openai|created by openai",
            "de ETHCali",
            text,
            flags=re.IGNORECASE,
        )
    return text


def is_generic_identity_reply(text: str) -> bool:
    lower = text.strip().lower()
    options = {
        f"soy {BOT_NAME.lower()}. ¿en qué te ayudo?",
        f"soy {BOT_NAME.lower()}. ¿en qué te ayudo hoy?",
        f"soy {BOT_NAME.lower()}. en que te ayudo?",
        f"soy {BOT_NAME.lower()}. en que te ayudo hoy?",
        f"i am {BOT_NAME.lower()}. how can i help?",
    }
    return lower in options


def is_crypto_price_query(user_message: str) -> bool:
    text = user_message.lower().strip()
    asks_price = bool(
        re.search(
            r"\b(precio|price|valor|cotiza|cotizacion|cotización|cuanto vale|cuánto vale|en cuanto|en cuánto)\b",
            text,
        )
    )
    if not asks_price:
        return False

    if any(word in text for word in ["cripto", "crypto", "criptomoneda", "coin", "token"]):
        return True

    if re.search(r"\$(?:[a-z]{2,10})\b", text):
        return True

    return bool(
        re.search(
            r"\b(btc|bitcoin|eth|ethereum|sol|solana|xrp|bnb|doge|ada|ltc|dot|trx|avax|link|matic|usdt|usdc|shib)\b",
            text,
        )
    )


def is_who_we_are_query(user_message: str) -> bool:
    text = user_message.lower().strip()
    return bool(
        re.search(
            r"\b(quienes somos|quiénes somos|quienes son|quiénes son|who are we|about us)\b",
            text,
        )
    )


def detect_crypto_symbol(user_message: str) -> Optional[str]:
    text_lower = user_message.lower()

    for alias, symbol in CRYPTO_SYMBOL_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text_lower):
            return symbol

    symbol_with_dollar = re.search(r"\$([a-zA-Z]{2,10})\b", user_message)
    if symbol_with_dollar:
        return symbol_with_dollar.group(1).upper()

    for token in re.findall(r"\b[A-Z]{2,10}\b", user_message):
        if token not in {"USD", "COP", "EUR", "MXN"}:
            return token

    return None


def format_quote_price(price: float) -> str:
    if price >= 100:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:,.6f}"
    return f"${price:,.8f}"


def get_coinmarketcap_quote(symbol: str) -> str:
    if not COINMARKETCAP_API_KEY:
        raise RuntimeError("COINMARKETCAP_API_KEY no configurada")

    headers = {
        "X-CMC_PRO_API_KEY": COINMARKETCAP_API_KEY,
        "Accepts": "application/json",
    }
    params = {"symbol": symbol, "convert": COINMARKETCAP_CONVERT}
    response = requests.get(
        COINMARKETCAP_QUOTES_URL,
        headers=headers,
        params=params,
        timeout=(3, 10),
    )
    response.raise_for_status()
    payload = response.json()

    status = payload.get("status", {}) if isinstance(payload, dict) else {}
    if status.get("error_code", 0) != 0:
        raise RuntimeError(status.get("error_message", "Error de CoinMarketCap"))

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    quote_data = data.get(symbol.upper())
    if isinstance(quote_data, list):
        quote_data = quote_data[0] if quote_data else None
    if not isinstance(quote_data, dict):
        raise RuntimeError(f"No encontre datos para {symbol.upper()}")

    name = quote_data.get("name", symbol.upper())
    symbol_value = quote_data.get("symbol", symbol.upper())
    quote = quote_data.get("quote", {}).get(COINMARKETCAP_CONVERT, {})
    price = quote.get("price")
    change_24h = quote.get("percent_change_24h")

    if price is None:
        raise RuntimeError("CoinMarketCap no devolvio precio")

    price_text = format_quote_price(float(price))
    change_text = "N/D"
    if isinstance(change_24h, (int, float)):
        sign = "+" if change_24h >= 0 else ""
        change_text = f"{sign}{change_24h:.2f}%"

    return (
        f"{name} ({symbol_value})\n"
        f"Precio actual: {price_text} {COINMARKETCAP_CONVERT}\n"
        f"Cambio 24h: {change_text}\n"
        "Fuente: CoinMarketCap"
    )


def get_crypto_price_response(user_message: str) -> str:
    symbol = detect_crypto_symbol(user_message)
    if not symbol:
        return (
            "Dime el ticker o nombre de la cripto (por ejemplo: BTC, ETH, SOL) "
            "y te consulto su precio actual."
        )

    try:
        return get_coinmarketcap_quote(symbol)
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {401, 403}:
            return "La clave de CoinMarketCap no es valida o no tiene permisos."
        if status_code == 429:
            return "CoinMarketCap reporta limite de solicitudes. Intenta de nuevo en un momento."
        return f"No pude consultar CoinMarketCap ahora (HTTP {status_code})."
    except Exception as exc:
        return f"No pude consultar el precio de {symbol} en este momento: {exc}"


def build_local_fallback_reply(user_message: str) -> str:
    text = user_message.strip()
    lower = text.lower()

    if any(g in lower for g in ["hola", "buenas", "hey", "que tal", "qué tal"]):
        return f"Hola, soy {BOT_NAME}. Estoy activo. Envíame una sola pregunta y te ayudo."

    math_match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([+\-*/])\s*(-?\d+(?:\.\d+)?)\s*", text)
    if math_match:
        left = float(math_match.group(1))
        op = math_match.group(2)
        right = float(math_match.group(3))
        if op == "+":
            result = left + right
        elif op == "-":
            result = left - right
        elif op == "*":
            result = left * right
        else:
            if right == 0:
                return "No puedo dividir por cero."
            result = left / right
        if result.is_integer():
            return str(int(result))
        return f"{result:.6g}"

    if lower.startswith("que es ") or lower.startswith("qué es "):
        term = text.split(" ", 2)[-1].strip(" ?")
        if term:
            return (
                f"{term.capitalize()} es un concepto que puedo explicarte mejor cuando el motor externo responda. "
                "Si quieres, te doy una version corta por partes."
            )

    return (
        "Sigo activo. Ahora mismo el proveedor de IA esta saturado; "
        "envia una pregunta corta (una sola idea) y te respondo apenas libere cola."
    )


def generate_reply(chat_id: int, user_message: str) -> str:
    if is_who_we_are_query(user_message):
        return WHO_WE_ARE_URL

    if is_crypto_price_query(user_message):
        return get_crypto_price_response(user_message)

    backends = []
    if USE_OPENCLAW_CLI:
        backends.append(("openclaw-cli", lambda: ask_openclaw_cli(chat_id, user_message)))
    elif USE_OPENCLAW_HTTP:
        backends.append(("openclaw-http", lambda: ask_openclaw_http(user_message)))
    backends.append(("pollinations-openai", lambda: ask_fallback_llm(user_message)))
    backends.append(("pollinations-legacy", lambda: ask_fallback_llm_legacy(user_message)))

    for backend_name, backend_call in backends:
        if not backend_is_available(backend_name):
            continue
        try:
            reply = backend_call()
            register_backend_success(backend_name)
            normalized_reply = normalize_identity(reply)
            if not normalized_reply or is_generic_identity_reply(normalized_reply):
                raise RuntimeError("Respuesta vacia o identidad generica")
            return normalized_reply
        except Exception as exc:
            register_backend_failure(backend_name, exc)
            logger.warning("Fallo en backend '%s': %s", backend_name, exc)

    return normalize_identity(build_local_fallback_reply(user_message))


async def reply_text_safe(update: Update, text: str) -> None:
    if not update.message:
        return

    message_text = (text or "").strip() or "Sin respuesta."
    chunks = [
        message_text[i : i + MAX_TELEGRAM_MESSAGE_LEN]
        for i in range(0, len(message_text), MAX_TELEGRAM_MESSAGE_LEN)
    ]
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(f"Hola, soy {BOT_NAME}. ¿En qué te ayudo hoy?")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Envíame cualquier mensaje y te respondo.\n"
        "Comandos: /start, /help"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_message = update.message.text
    chat_id = update.effective_chat.id if update.effective_chat else 0
    try:
        async with get_chat_lock(chat_id):
            reply = await asyncio.to_thread(generate_reply, chat_id, user_message)
            await reply_text_safe(update, reply)
    except Exception:
        logger.exception("Error procesando mensaje en chat_id=%s", chat_id)
        await reply_text_safe(
            update,
            "Ocurrió un error interno procesando tu mensaje. Intenta nuevamente.",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error no controlado en update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Ocurrió un error inesperado. Intenta de nuevo."
            )
        except Exception:
            logger.exception("No se pudo enviar mensaje de error al usuario")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "TU_TOKEN_AQUI":
        raise RuntimeError(
            "Define TELEGRAM_TOKEN como variable de entorno con el token real."
        )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print("Iniciando bot de Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()

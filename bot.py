"""
ForexLotSizeBot
----------------
A Telegram bot that calculates the correct position (lot) size for a forex
trade based on account balance, risk percentage, stop-loss distance (pips),
and the currency pair being traded.

Run locally:
    export BOT_TOKEN="123456:ABC..."
    python bot.py

Deploy: see README.md (Railway + GitHub instructions).
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Config & logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
STANDARD_LOT_UNITS = 100_000

# Conversation states
BALANCE, RISK, STOP_LOSS, PAIR = range(4)

POPULAR_PAIRS = [
    ["EUR/USD", "GBP/USD", "USD/JPY"],
    ["AUD/USD", "USD/CAD", "USD/CHF"],
    ["NZD/USD", "EUR/GBP", "EUR/JPY"],
]


# --------------------------------------------------------------------------- #
# Pip value / lot size math
# --------------------------------------------------------------------------- #

@dataclass
class LotResult:
    pair: str
    lots_standard: float
    lots_mini: float
    lots_micro: float
    units: float
    risk_amount: float
    pip_value_per_standard_lot: float


async def fetch_rate_to_usd(currency: str) -> float:
    """Return how many USD 1 unit of `currency` is worth (free, no API key)."""
    currency = currency.upper()
    if currency == "USD":
        return 1.0
    url = f"https://api.frankfurter.app/latest?from={currency}&to=USD"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return float(data["rates"]["USD"])


def parse_pair(raw: str) -> tuple[str, str]:
    raw = raw.upper().replace(" ", "")
    if "/" in raw:
        base, quote = raw.split("/", 1)
    elif len(raw) == 6:
        base, quote = raw[:3], raw[3:]
    else:
        raise ValueError("Could not parse currency pair. Use format like EUR/USD.")
    return base, quote


async def calculate_lot_size(
    balance: float,
    risk_percent: float,
    stop_loss_pips: float,
    pair_raw: str,
) -> LotResult:
    base, quote = parse_pair(pair_raw)
    pip_size = 0.01 if quote == "JPY" else 0.0001

    rate_quote_to_usd = await fetch_rate_to_usd(quote)
    pip_value_per_standard_lot = pip_size * STANDARD_LOT_UNITS * rate_quote_to_usd

    risk_amount = balance * (risk_percent / 100)
    lots_standard = risk_amount / (stop_loss_pips * pip_value_per_standard_lot)
    units = lots_standard * STANDARD_LOT_UNITS

    return LotResult(
        pair=f"{base}/{quote}",
        lots_standard=lots_standard,
        lots_mini=lots_standard * 10,
        lots_micro=lots_standard * 100,
        units=units,
        risk_amount=risk_amount,
        pip_value_per_standard_lot=pip_value_per_standard_lot,
    )


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #

WELCOME = (
    "👋 *ForexLotSizeBot*\n\n"
    "I calculate the correct position size for a trade based on your account "
    "risk.\n\n"
    "Commands:\n"
    "/calculate — step-by-step calculator\n"
    "/calc 10000 1 25 EUR/USD — quick one-line calc "
    "(balance, risk%, stop-loss pips, pair)\n"
    "/cancel — cancel current calculation\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(WELCOME)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Cancelled.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ---- Step-by-step conversation ---- #

async def calculate_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "💰 What is your *account balance* (in USD)?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return BALANCE


async def got_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        balance = float(update.message.text.replace(",", "").strip())
        if balance <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "That doesn't look like a valid number. Enter your balance again, e.g. 10000"
        )
        return BALANCE

    context.user_data["balance"] = balance
    await update.message.reply_text(
        "📊 What *% of your account* are you willing to risk on this trade? (e.g. 1)",
        parse_mode="Markdown",
    )
    return RISK


async def got_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        risk = float(update.message.text.strip())
        if not (0 < risk <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Enter a risk percentage between 0 and 100, e.g. 1 or 2.5"
        )
        return RISK

    context.user_data["risk"] = risk
    await update.message.reply_text(
        "🛑 What is your *stop-loss distance in pips*? (e.g. 25)",
        parse_mode="Markdown",
    )
    return STOP_LOSS


async def got_stop_loss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        sl = float(update.message.text.strip())
        if sl <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter stop-loss pips as a positive number, e.g. 25")
        return STOP_LOSS

    context.user_data["sl"] = sl
    keyboard = ReplyKeyboardMarkup(POPULAR_PAIRS, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "💱 Which *currency pair*? (e.g. EUR/USD) — pick one below or type your own",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return PAIR


async def got_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pair_raw = update.message.text.strip()
    balance = context.user_data["balance"]
    risk = context.user_data["risk"]
    sl = context.user_data["sl"]

    await update.message.reply_text(
        "Crunching numbers…", reply_markup=ReplyKeyboardRemove()
    )

    try:
        result = await calculate_lot_size(balance, risk, sl, pair_raw)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\nTry /calculate again.")
        return ConversationHandler.END
    except (httpx.HTTPError, KeyError):
        await update.message.reply_text(
            "⚠️ Couldn't fetch live exchange rates right now. Please try again shortly."
        )
        return ConversationHandler.END

    await update.message.reply_markdown(format_result(result, balance, risk, sl))
    return ConversationHandler.END


def format_result(result: LotResult, balance: float, risk: float, sl: float) -> str:
    return (
        f"📈 *Lot Size Result — {result.pair}*\n\n"
        f"Account balance: ${balance:,.2f}\n"
        f"Risk: {risk}% (${result.risk_amount:,.2f})\n"
        f"Stop-loss: {sl} pips\n\n"
        f"*Standard lots:* `{result.lots_standard:.2f}`\n"
        f"*Mini lots:* `{result.lots_mini:.2f}`\n"
        f"*Micro lots:* `{result.lots_micro:.2f}`\n"
        f"Units: `{result.units:,.0f}`\n\n"
        f"_Pip value/standard lot ≈ ${result.pip_value_per_standard_lot:.2f}_"
    )


# ---- Quick one-line command: /calc 10000 1 25 EUR/USD ---- #

async def quick_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 4:
        await update.message.reply_text(
            "Usage: /calc <balance> <risk%> <stop_loss_pips> <pair>\n"
            "Example: /calc 10000 1 25 EUR/USD"
        )
        return

    try:
        balance = float(args[0].replace(",", ""))
        risk = float(args[1])
        sl = float(args[2])
        pair_raw = args[3]
        if balance <= 0 or sl <= 0 or not (0 < risk <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Check your inputs: balance and stop-loss must be positive, "
            "risk% must be between 0 and 100."
        )
        return

    try:
        result = await calculate_lot_size(balance, risk, sl, pair_raw)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    except (httpx.HTTPError, KeyError):
        await update.message.reply_text(
            "⚠️ Couldn't fetch live exchange rates right now. Please try again shortly."
        )
        return

    await update.message.reply_markdown(format_result(result, balance, risk, sl))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)


# --------------------------------------------------------------------------- #
# App entrypoint
# --------------------------------------------------------------------------- #

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Get a token from @BotFather on Telegram and set it."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("calc", quick_calc))

    conv = ConversationHandler(
        entry_points=[CommandHandler("calculate", calculate_entry)],
        states={
            BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_balance)],
            RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_risk)],
            STOP_LOSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_stop_loss)],
            PAIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_pair)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_error_handler(error_handler)

    return app


def main() -> None:
    app = build_app()
    logger.info("Starting ForexLotSizeBot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

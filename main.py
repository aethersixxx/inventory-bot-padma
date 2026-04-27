"""
Entry point Inventory Telegram Bot.
"""
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from src import config, handlers
from src.logger import logger


def main() -> None:
    config.validate()
    logger.info("=" * 60)
    logger.info("Starting Inventory Bot")
    logger.info("Admins: %s", config.ADMIN_USER_IDS or "(none)")
    logger.info("Allowed users: %s", config.ALLOWED_USER_IDS or "(public read)")
    logger.info("Cache TTL: %ds", config.CACHE_TTL)
    logger.info("=" * 60)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("whoami", handlers.cmd_whoami))
    app.add_handler(CommandHandler("search", handlers.cmd_search))
    app.add_handler(CommandHandler("all", handlers.cmd_all))
    app.add_handler(CommandHandler("update", handlers.cmd_update))
    app.add_handler(CommandHandler("refresh", handlers.cmd_refresh))

    # Free text → search
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text)
    )

    # Error handler
    app.add_error_handler(handlers.on_error)

    logger.info("Bot polling started...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()

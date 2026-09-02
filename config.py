import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "shop.db")

DEFAULT_SETTINGS = {
    "brand_name": "VPN Service",
    "brand_emoji": "🛡️",
    "brand_subtitle": "Быстрый и безопасный VPN",
    "panel_url": os.environ.get("PANEL_URL", "http://127.0.0.1:5000"),
    "panel_token": os.environ.get("PANEL_TOKEN", ""),
    "platega_merchant_id": os.environ.get("PLATEGA_MERCHANT_ID", ""),
    "platega_secret": os.environ.get("PLATEGA_SECRET", ""),
    # Platega test mode (uses separate test credentials + URL for sandbox)
    "platega_test_mode": os.environ.get("PLATEGA_TEST_MODE", "False"),
    "platega_test_merchant_id": os.environ.get("PLATEGA_TEST_MERCHANT_ID", ""),
    "platega_test_secret": os.environ.get("PLATEGA_TEST_SECRET", ""),
    "platega_test_base_url": os.environ.get("PLATEGA_TEST_BASE_URL", "https://sandbox.platega.io"),
    "shop_public_url": os.environ.get("SHOP_PUBLIC_URL", "http://127.0.0.1:8080"),
    "test_subscription_days": 3,
    # Referral system
    "referral_enabled": True,
    "referral_threshold": 100,          # min RUB of first deposit to trigger rewards
    "referral_bonus_referee": 100,      # RUB bonus credited to the referee
    "referral_bonus_referrer": 100,     # RUB reward credited to the referrer (first time)
    "referral_commission_percent": 25,  # % commission earned on each subsequent deposit
    # Telegram bot
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
}

SESSION_SECRET = os.environ.get("SHOP_SECRET_KEY", "please-change-me")

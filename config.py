import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BOT_TOKEN         = os.getenv("BOT_TOKEN")
DRIVER_BOT_TOKEN  = os.getenv("DRIVER_BOT_TOKEN")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL      = os.getenv("DATABASE_URL")

USDT_ADDRESS      = os.getenv("USDT_ADDRESS", "TJnJXC7arrvDVDSqybHcs8MkkdwUncZ1Tp")
MIN_PRICE         = float(os.getenv("MIN_PRICE", "3.5"))
USD_TO_EUR        = float(os.getenv("USD_TO_EUR", "0.86"))
USD_TO_TRY        = float(os.getenv("USD_TO_TRY", "58.17"))

SEARCH_TIMEOUT           = int(os.getenv("SEARCH_TIMEOUT", "120"))
MAX_CANCELS_IN_WINDOW    = int(os.getenv("MAX_CANCELS_IN_WINDOW", "5"))
CANCEL_WINDOW_MINUTES    = int(os.getenv("CANCEL_WINDOW_MINUTES", "15"))
CANCEL_BAN_HOURS         = int(os.getenv("CANCEL_BAN_HOURS", "2"))
FLOOD_INTERVAL_SECONDS   = float(os.getenv("FLOOD_INTERVAL_SECONDS", "3"))
DRIVER_DELAY_NOTIFY_MINUTES = int(os.getenv("DRIVER_DELAY_NOTIFY_MINUTES", "10"))

ZONE = {
    "min_lat": float(os.getenv("ZONE_MIN_LAT", "35.10")),
    "max_lat": float(os.getenv("ZONE_MAX_LAT", "35.45")),
    "min_lon": float(os.getenv("ZONE_MIN_LON", "33.60")),
    "max_lon": float(os.getenv("ZONE_MAX_LON", "34.20")),
}

PROMO_CODES = {
    "RYDEX10": {"discount": 1.0},
    "WELCOME":  {"discount": 1.5},
}

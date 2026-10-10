import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    """Application configuration."""

    # Flask Core & Security
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", os.getenv("SECRET_KEY", "seismic-vulnerability-dev-secret-key-2026"))
    DEBUG = os.getenv("FLASK_DEBUG", "True").lower() in ("true", "1", "yes")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "False").lower() in ("true", "1", "yes")
    WTF_CSRF_ENABLED = os.getenv("WTF_CSRF_ENABLED", "True").lower() in ("true", "1", "yes")
    WTF_CSRF_TIME_LIMIT = int(os.getenv("WTF_CSRF_TIME_LIMIT", "3600"))

    # File Uploads
    UPLOAD_FOLDER = BASE_DIR / "uploads"
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))  # 16 MB max
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

    # Database
    DATABASE_PATH = BASE_DIR / os.getenv("DATABASE_NAME", "seismic_reports.db")

    # Gemma Vision Model Configuration
    # Supports Google AI Studio / Gemini endpoint or OpenAI-compatible endpoints (Ollama, vLLM, OpenRouter)
    GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "").strip()
    GEMMA_MODEL_NAME = os.getenv("GEMMA_MODEL_NAME", "gemma-4-26b-a4b-it").strip()
    GEMMA_API_URL = os.getenv(
        "GEMMA_API_URL",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    ).strip()
    # Provider options: 'auto', 'google_genai', 'openai_compatible'
    GEMMA_PROVIDER = os.getenv("GEMMA_PROVIDER", "auto").strip().lower()

    # When true and GEMMA_API_KEY is not set or network fails, use an offline structural analysis simulation
    GEMMA_MOCK_FALLBACK = os.getenv("GEMMA_MOCK_FALLBACK", "true").lower() in ("true", "1", "yes")

    # Location-based Hazard Clustering
    CLUSTER_RADIUS_METERS = int(os.getenv("CLUSTER_RADIUS_METERS", "500"))  # meters for nearby clustering


# Ensure upload directory exists
os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

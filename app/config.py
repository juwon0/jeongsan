import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("JEONGSAN_MODEL", "gemma4:e4b")
DEFAULT_CURRENCY = os.getenv("JEONGSAN_CURRENCY", "KRW")  # for receipts that don't print one
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

MODEL_DOWN = "The local model did not answer. Is Ollama running with the model pulled?"

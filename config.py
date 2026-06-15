import os

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
JWT_EXPIRES_HOURS = 12

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "wb_dashboard.db")

ADMIN_LOGIN = os.environ.get("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# Fernet key for encrypting WB API tokens at rest.
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# If not set, tokens are stored plaintext (warn on startup).
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

# Allowed origin for CORS (set to your public domain in prod)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://wb-dashboard-moodsduck.amvera.io")

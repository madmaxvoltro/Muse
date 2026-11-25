import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

# Every test runs with encryption ON (a throwaway key), so the whole pipeline is exercised the
# way it runs in production.
import os

from muse_schema.crypto import generate_key_file, reset_vault_cache

os.environ.setdefault("MUSE_MASTER_KEY", generate_key_file())
os.environ.pop("MUSE_ENCRYPTION", None)
reset_vault_cache()
os.environ['MUSE_ALLOW_OPEN_READ'] = 'true'  # route tests call the API without keys

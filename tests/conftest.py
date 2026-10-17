import os
import sys
from pathlib import Path

os.environ.setdefault("KEYCLOAK_URL", "http://kc")
os.environ.setdefault("KEYCLOAK_ADMIN_USERNAME", "a")
os.environ.setdefault("KEYCLOAK_ADMIN_PASSWORD", "p")
sys.path.insert(0, str(Path(__file__).parent.parent / "controller"))

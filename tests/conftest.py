"""Make the add-on's app/ modules importable the same way the container does."""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "bosch_ebike_ldi" / "app"
sys.path.insert(0, str(APP_DIR))

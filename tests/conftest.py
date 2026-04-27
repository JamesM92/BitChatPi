import sys
from pathlib import Path

# bitchatd library now lives under server/
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

# tests/faithfulness/conftest.py
# Make `import api` / `import api.pipeline` work when running pytest from repo root.
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]  # .../tests/faithfulness -> repo root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

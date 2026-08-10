"""Runtime hook: runs before the main script. Sets BYPASS_LICENSE=1 so that
``utils/license.py`` sees the env var at import time and skips license checks."""
import os

os.environ["BYPASS_LICENSE"] = "1"

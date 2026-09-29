"""
Vivaran-VQE - backward compatibility for settings from the old name (Antah.ai).

Every setting is now read from VIVARAN_* environment variables. Older start
scripts that still export ANTAHAI_* keep working: any ANTAHAI_X that is set
is copied to VIVARAN_X unless VIVARAN_X is already set. Imported first by
settings.py and db.py.
"""

import os

for _key, _value in list(os.environ.items()):
    if _key.startswith("ANTAHAI_"):
        os.environ.setdefault("VIVARAN_" + _key[len("ANTAHAI_"):], _value)

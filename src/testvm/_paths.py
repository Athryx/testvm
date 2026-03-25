from __future__ import annotations

import os
import platform
from pathlib import Path

from platformdirs import user_data_path

DATA_DIR_ENV_VAR = "TESTVM_DATA_DIR"
APP_NAME = "testvm"


def get_data_dir(create: bool = True) -> Path:
    env_value = os.environ.get(DATA_DIR_ENV_VAR)
    if env_value:
        path = Path(env_value).expanduser()
    elif platform.system() == "Linux":
        path = Path.home() / ".local" / "var" / APP_NAME
    else:
        path = user_data_path(APP_NAME, appauthor=False)

    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path

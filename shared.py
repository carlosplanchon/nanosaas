#!/usr/bin/env python3

from pathlib import Path

import tomllib


##################
# --- CONFIG --- #
##################
with Path("config.toml").open(mode="rb") as fp:
    CONFIG = tomllib.load(fp)


DB_USERNAME: str = CONFIG["database"]["DB_USERNAME"]
DB_PASSWORD: str = CONFIG["database"]["DB_PASSWORD"]
DB_HOST: str = CONFIG["database"]["DB_HOST"]
DB_PORT: str = CONFIG["database"]["DB_PORT"]
DB_NAME: str = CONFIG["database"]["DB_NAME"]

DB_URL: str =\
    f"postgres://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

TORTOISE_ORM = {
    "connections": {
        "default": DB_URL,
    },
    "apps": {
        "models": {
            "models": ["web.db_models", "aerich.models"],
            "default_connection": "default",
        }
    }
}

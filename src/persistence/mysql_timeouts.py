import os


def mysql_timeout_connect_args(prefix: str) -> dict[str, int]:
    defaults = {
        "CONNECT_TIMEOUT_SECONDS": 10,
        "READ_TIMEOUT_SECONDS": 60,
        "WRITE_TIMEOUT_SECONDS": 60,
    }
    values = {}
    for suffix, default in defaults.items():
        env_name = f"{prefix}_{suffix}"
        raw_value = os.getenv(env_name)
        value = default if raw_value in (None, "") else int(raw_value)
        if value <= 0:
            raise ValueError(f"{env_name} must be a positive integer")
        values[suffix.removesuffix("_SECONDS").lower()] = value
    return values

from .initial import initial_settings
from .final import Settings
from typing import Any, Callable
from loguru import logger

__all__ = ["config"]

config: Settings = None # type: ignore

if initial_settings.load_config:
    # import stuff needed to load the config
    from deepmerge import always_merger
    import sys
    
    configs: list[dict[str, Any]] = []
    load_config: Callable[[str], dict] # without this mypy will error about param names

    # load the config
    if initial_settings.file is not None:
        logger.info(f"Loading config from {initial_settings.file}")
        from .file import load_config
        configs.append(load_config(initial_settings.file))

    if initial_settings.http_url is not None:
        logger.info(f"Loading config from {initial_settings.http_url}")
        from .http import load_config
        configs.append(load_config(initial_settings.http_url))

    if initial_settings.config_json is not None:
        logger.info("Loading config from json string")
        from json import loads as load_config
        configs.append(load_config(initial_settings.config_json))

    # merge the configs
    result: dict = {}
    for cfg in configs:
        always_merger.merge(result, cfg)

    # build the config
    config = Settings(**result)

    logger.remove()
    logger.add(sys.stderr, format="{time} {level} {message}", level=config.logging.log_level)
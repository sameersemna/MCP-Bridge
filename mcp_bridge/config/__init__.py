from mcp_bridge.config.env_subst import substitute_env_vars
from mcp_bridge.config.initial import initial_settings
from mcp_bridge.config.final import Settings
from typing import Any, Callable
from loguru import logger
from pydantic import ValidationError
from mcp_bridge.logging import configure_logging

__all__ = ["config"]

config: Settings = None  # type: ignore

if initial_settings.load_config:
    # import stuff needed to load the config
    try:
        from deepmerge import always_merger
    except ImportError:  # pragma: no cover - fallback for minimal environments
        def always_merger() -> dict[str, Any]:
            return {}

    configs: list[dict[str, Any]] = []
    load_config: Callable[[str], dict]  # without this mypy will error about param names

    # load the config
    if initial_settings.file is not None:
        logger.info(f"Loading config from {initial_settings.file}")
        from .file import load_config

        configs.append(load_config(initial_settings.file))

    if initial_settings.http_url is not None:
        logger.info(f"Loading config from {initial_settings.http_url}")
        from .http import load_config

        configs.append(load_config(initial_settings.http_url))

    if initial_settings.json is not None:
        logger.info("Loading config from json string")
        configs.append(initial_settings.json)

    # merge the configs
    result: dict = {}
    for cfg in configs:
        if "always_merger" in globals() and callable(always_merger):
            try:
                always_merger.merge(result, cfg)
            except AttributeError:
                result = {**result, **cfg}
        else:
            result = {**result, **cfg}

    result = substitute_env_vars(result)

    # build the config
    try:
        config = Settings(**result)
    except ValidationError as e:
        logger.error("unable to load a valid configuration")
        for error in e.errors():
            logger.error(f"{error['loc'][0]}: {error['msg']}")
        exit(1)

    configure_logging(config.logging.log_level)

from dotenv import load_dotenv

# Load the `.env` file into the process environment *before* importing the
# config and any module that reads `os.getenv` directly (e.g. the persistent
# tool cache reads `MCP_BRIDGE_TOOL_CACHE_*` via `os.getenv`). pydantic-settings
# reads `.env` for its own fields, but does NOT inject them into `os.environ`,
# so this explicit load is required for `os.getenv`-based settings to work.
# Real environment variables take precedence over the `.env` file.
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from mcp_bridge import __version__ as version
from mcp_bridge.config import config
from mcp_bridge.routers import secure_router, public_router
from mcp_bridge.lifespan import lifespan
from mcp_bridge.openapi_tags import tags_metadata
from mcp_bridge.telemetry import setup_tracing


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
    app = FastAPI(
        title="MCP Bridge",
        description="A middleware application to add MCP support to OpenAI-compatible APIs",
        version=version,
        lifespan=lifespan,
        openapi_tags=tags_metadata,
    )

    # setup tracing
    setup_tracing(app)

    # show auth data
    if config.security.auth.enabled:
        logger.info("Authentication is enabled")
    else:
        logger.info("Authentication is disabled")

    # Add CORS middleware
    if config.security.CORS.enabled:
        if config.security.CORS.allow_origins == ["*"]:
            logger.info("CORS middleware is enabled with wildcard origins")
        else:
            logger.info("CORS middleware is enabled")

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.security.CORS.allow_origins,
            allow_credentials=config.security.CORS.allow_credentials,
            allow_methods=config.security.CORS.allow_methods,
            allow_headers=config.security.CORS.allow_headers,
        )
    else:
        logger.info("CORS middleware is disabled")

    app.include_router(secure_router)
    app.include_router(public_router)

    return app

app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        "mcp_bridge.main:get_app",
        host=config.network.host,
        port=config.network.port,
        reload=False,
        factory=True,
    )

if __name__ == "__main__":
    run()
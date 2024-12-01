from fastapi import FastAPI
from endpoints import router
from lifespan import lifespan

app = FastAPI(
    title="MCP Bridge",
    description="A middleware application to add MCP support to openai compatible apis",
    lifespan=lifespan,
)

app.include_router(router)

from fastapi import FastAPI
from endpoints import router as endpointRouter
from mcp_endpoints import router as mcpRouter
from lifespan import lifespan

app = FastAPI(
    title="MCP Bridge",
    description="A middleware application to add MCP support to openai compatible apis",
    lifespan=lifespan,
)

app.include_router(endpointRouter)
app.include_router(mcpRouter)

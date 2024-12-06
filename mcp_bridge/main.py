from fastapi import FastAPI
from endpoints import router as endpointRouter
from mcp_endpoints import router as mcpRouter
from lifespan import lifespan
from openapi_tags import tags_metadata

app = FastAPI(
    title="MCP Bridge",
    description="A middleware application to add MCP support to openai compatible apis",
    lifespan=lifespan,
    openapi_tags=tags_metadata,
)

app.include_router(endpointRouter)
app.include_router(mcpRouter)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

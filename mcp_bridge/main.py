from fastapi import FastAPI
from endpoints import router

app = FastAPI(
    title="MCP Bridge",
    description="A middleware application to add MCP support to openai compatible apis",
)

app.include_router(router)

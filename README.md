# MCP-Bridge
A middleware to provide an openAI compatible endpoint that can call MCP tools

## Installation
1. Install the requirements
2. Create a `.env` file in the root directory of the project with the following variables:
    - `MCP_BRIDGE__INFERENCE_SERVER__BASE_URL`: The base URL of the inference server
    - `MCP_BRIDGE__INFERENCE_SERVER__API_KEY`: The API key for the inference server (optional)
3. Run `uvicorn.exe --app-dir .\mcp_bridge\ main:app`


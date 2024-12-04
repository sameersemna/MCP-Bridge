# MCP-Bridge
A middleware to provide an openAI compatible endpoint that can call MCP tools. MCP-Bridge acts as a bridge between the OpenAI API and MCP (MCP) tools, allowing developers to leverage MCP tools through the OpenAI API interface.

## Overview
MCP-Bridge is designed to facilitate the integration of MCP tools with the OpenAI API. It provides a set of endpoints that can be used to interact with MCP tools in a way that is compatible with the OpenAI API. This allows developers to use MCP tools in their applications without needing to understand the underlying MCP API.

## current features
- non streaming chat completions with MCP
- non streaming completions without MCP

- streaming chat completions are not implemented yet
- streaming completionsare not implemented yet

## Installation

The recommended way to install MCP-Bridge is to use Docker. See the example compose.yml file for an example of how to set up docker.

1. **Clone the repository**

2. **Edit the compose.yml file**

3. **run the service**
```
docker-compose up --build -d
```

### To manually install MCP-Bridge, follow these steps:

1. **Clone the repository**

2. **Set up a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install the requirements:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Create a `.env` file in the root directory of the project with the following variables:**
   - `MCP_BRIDGE__INFERENCE_SERVER__BASE_URL`: The base URL of the inference server
   - `MCP_BRIDGE__INFERENCE_SERVER__API_KEY`: The API key for the inference server (optional)

5. **Run the application:**
   ```bash
   uvicorn mcp_bridge.main:app --reload
   ```

## Usage
Once the application is running, you can interact with it using the OpenAI API. Here is a quick example of how to use the `chatCompletion` endpoint.

New MCP servers can be added by updating the environment variables `.env` file, and the tools are injected into the completion call automatically.

## Adding New MCP Servers
To add new MCP servers, you need to update the environment variables `.env` file. Here is an example of how to add a new MCP server:
```env
MCP_BRIDGE__MCP_SERVERS__FETCH__COMMAND=uvx # any command in path
MCP_BRIDGE__MCP_SERVERS__FETCH__ARGS=["mcp-server-fetch"] # json array
```

## Contribution Guidelines
Contributions to MCP-Bridge are welcome! To contribute, please follow these steps:
1. Fork the repository.
2. Create a new branch for your feature or bug fix.
3. Make your changes and commit them.
4. Push your changes to your fork.
5. Create a pull request to the main repository.

## License
MCP-Bridge is licensed under the MIT License. See the [LICENSE](LICENSE) file for more information.

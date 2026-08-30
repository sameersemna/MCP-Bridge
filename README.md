# MCP-Bridge

<p>
  <a href="https://discord.gg/4NVQHqNxSZ"><img alt="Discord" src="https://img.shields.io/discord/1320517159331430480?style=flat&logo=discord&color=blue"></a>
  <a href="/docs/README.md"><img alt="Static Badge" src="https://img.shields.io/badge/docs-md-blue"></a>
  <a href="LICENSE"><img alt="Static Badge" src="https://img.shields.io/badge/License-MIT-blue?style=flat"></a>
</p>


MCP-Bridge acts as a bridge between the OpenAI API and MCP (MCP) tools, allowing developers to leverage MCP tools through the OpenAI API interface.

> [!NOTE]
> 
>  Looking for new maintainers to assist with the project. Reach out in the Discord or open an issue if you are interested.
>
>  Additionally, Open WebUI natively supports MCP (Model Context Protocol) starting in v0.6.31, so MCP-Bridge should be considered as soft deprecated now.

## Overview
MCP-Bridge is designed to facilitate the integration of MCP tools with the OpenAI API. It provides a set of endpoints that can be used to interact with MCP tools in a way that is compatible with the OpenAI API. This allows you to use any client with any MCP tool without explicit support for MCP. For example, see this example of using Open Web UI with the official MCP fetch tool. 

![open web ui example](/assets/owui_example.png)

## Current Features

working features:

- non streaming chat completions with MCP
- streaming chat completions with MCP

- non streaming completions without MCP

- MCP tools
- MCP sampling

- SSE Bridge for external clients

planned features:

- streaming completions are not implemented yet

- MCP resources are planned to be supported

## Installation

The recommended way to install MCP-Bridge is to use Docker. See the example compose.yml file for an example of how to set up docker. 

Note that this requires an inference engine with tool call support. I have tested this with vLLM with success, though ollama should also be compatible.

### Docker installation

1. **Clone the repository**

2. **Edit the compose.yml file**

You will need to add a reference to the config.json file in the compose.yml file. Pick any of
- add the config.json file to the same directory as the compose.yml file and use a volume mount (you will need to add the volume manually)
- add a http url to the environment variables to download the config.json file from a url
- add the config json directly as an environment variable

see below for an example of each option:
```bash
environment:
  - MCP_BRIDGE__CONFIG__FILE=config.json # mount the config file for this to work
  - MCP_BRIDGE__CONFIG__HTTP_URL=http://10.88.100.170:8888/config.json
  - MCP_BRIDGE__CONFIG__JSON={"inference_server":{"base_url":"http://example.com/v1","api_key":"None"},"mcp_servers":{"fetch":{"command":"uvx","args":["mcp-server-fetch"]}}}
```
The mount point for using the config file would look like:
```yaml
    volumes:
      - ./config.json:/mcp_bridge/config.json
```

3. **run the service**
```
docker-compose up --build -d
```

### Manual installation (no docker)

If you want to run the application without docker, you will need to install the requirements and run the application manually.

1. **Clone the repository**

2. **Set up a dependencies:**
```bash
uv sync
```

3. **Create a config.json file in the root directory**

Here is an example config.json file:
```json
{
   "inference_server": {
      "base_url": "http://example.com/v1",
      "api_key": "None"
   },
   "mcp_servers": {
      "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "disabled": true
      }
   }
}
```

4. **Run the application:**
```bash
uv run mcp_bridge/main.py
```

## Usage
Once the application is running, you can interact with it using the OpenAI API.

View the documentation at [http://yourserver:8000/docs](http://localhost:8000/docs). There is an endpoint to list all the MCP tools available on the server, which you can use to test the application configuration.

## Rest API endpoints

MCP-Bridge exposes many rest api endpoints for interacting with all of the native MCP primatives. This lets you outsource the complexity of dealing with MCP servers to MCP-Bridge without comprimising on functionality. See the openapi docs for examples of how to use this functionality.

## SSE Bridge
MCP-Bridge also provides an SSE bridge for external clients. This lets external chat apps with explicit MCP support use MCP-Bridge as a MCP server. Point your client at the SSE endpoint (http://yourserver:8000/mcp-server/sse) and you should be able to see all the MCP tools available on the server.

This also makes it easy to test if your configuration is working correctly. You can use [wong2/mcp-cli](https://github.com/wong2/mcp-cli?tab=readme-ov-file#connect-to-a-running-server-over-sse) to test your configuration. `npx @wong2/mcp-cli --sse http://localhost:8000/mcp-server/sse`

If you want to use the tools inside of [claude desktop](https://claude.ai/download) or other `STDIO` only MCP clients, you can do this with a tool such as [lightconetech/mcp-gateway](https://github.com/lightconetech/mcp-gateway)

## Configuration

To add new MCP servers, edit the config.json file.

### Tool-loop environment variables

The tool-calling loop can be tuned with the following environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_BRIDGE_MAX_TOOL_TURNS` | `12` | Maximum number of tool-calling iterations per request. |
| `MCP_BRIDGE_MAX_CONTEXT_TOKENS` | *(model-derived)* | Hard override for the tool-loop context budget (tokens). If unset, the budget is derived per-model from the model's context window × `MCP_BRIDGE_CONTEXT_BUDGET_FRACTION`. When exceeded, the tool loop stops and a final answer is synthesized. Prevents runaway loops where the model keeps issuing tool calls and the context grows unboundedly. |
| `MCP_BRIDGE_CONTEXT_BUDGET_FRACTION` | `0.75` | Fraction of the model's context window used as the tool-loop budget. The remaining fraction is headroom for the final synthesized answer. |
| `MCP_BRIDGE_TOOL_TIMEOUT_SECONDS` | `60` | Per-tool-call timeout in seconds. |
| `MCP_BRIDGE_TOOL_RETRY_COUNT` | `0` | Number of retries for a timed-out tool call. |
| `MCP_BRIDGE_TOOL_RETRY_DELAY_SECONDS` | `0.25` | Delay between tool-call retries. |

### Model-aware context budget

The tool-loop context budget is **model-specific** rather than one-size-fits-all. The bridge derives it from the model's context window scaled by `MCP_BRIDGE_CONTEXT_BUDGET_FRACTION` (default 75%), so a 1M-context model gets a much larger budget than a 128k-context model. The context window is resolved in this order:

1. The local **`models.json` catalog** (generated from the provider's `/models` endpoint — see below).
2. The `inference_server.model_context_windows` map in `config.json` (exact model ID, or the short name after the last `/`).
3. A context hint embedded in the model ID (e.g. `model-200k`, `model-1m`).
4. A built-in lookup of known models.
5. A default of 128k tokens.

Example `config.json`:
```json
{
  "inference_server": {
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "sk-...",
    "model_context_windows": {
      "nvidia/nemotron-3-super-120b-a12b:free": 128000,
      "minimax/minimax-m3:free": 1000000
    }
  }
}
```

Setting `MCP_BRIDGE_MAX_CONTEXT_TOKENS` explicitly always overrides the derived budget.

### Tool-result caching

A model doing research often re-issues near-identical search queries (e.g. appending one keyword at a time: `"بدعة" "فتوى" ...` → `... "الكفار"` → `... "تشبه"`). Instead of blocking these (which would prevent legitimate multi-angle research), the bridge **caches tool results per request**. When a near-duplicate query is issued, the previously fetched result is returned instantly from the cache instead of re-fetching from the web — so the model can keep exploring from different angles without burning 30+ minutes on repeated web fetches.

- Two queries are "near-duplicates" when they share ≥4 terms and ≥80% of the smaller query's terms.
- Only successful (non-error) results are cached.
- The cache is per-request (in-memory) and bounded (64 entries), so it adds no cross-request staleness or unbounded memory.

### Persistent on-disk tool cache

In addition to the in-memory cache, the bridge keeps a **persistent on-disk cache** of tool results so repeat queries across requests (and across process restarts) are served from disk instead of re-fetching from the web — the biggest latency source. This also means that if a request fails halfway, the next attempt reuses the already-fetched results.

- Stored as one small JSON file per exact query (SHA-256 key) in a cache directory.
- **TTL** (default 48h) expires stale entries so search results don't go stale.
- Exact-match keys mean a lookup is a single file read — no RAM bloat, no scanning.
- Atomic writes (temp file + rename) keep concurrent requests safe.

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_BRIDGE_TOOL_CACHE_DIR` | `tool_cache/` (repo root) | Directory for the persistent tool cache. |
| `MCP_BRIDGE_TOOL_CACHE_TTL_SECONDS` | `172800` (48h) | How long a cached tool result stays valid. |
| `MCP_BRIDGE_TOOL_CACHE_ENABLED` | `true` | Set to `false` to disable the persistent cache. |

> **Docker note:** if you run MCP-Bridge in Docker, mount the cache directory so it persists on the host (otherwise it lives in the container's ephemeral filesystem and is lost on restart). Add to `compose.yml`:
> ```yaml
> volumes:
>   - ./tool_cache:/app/tool_cache
> ```

### Model catalog (`models.json`)

The bridge and the test harness read per-model details (context window, pricing, etc.) from a local `models.json` catalog. Generate it from the provider's `/models` endpoint:

```bash
python scripts/fetch_models.py
```

This writes `models.json` with every model's `context_length`, pricing, and modality. Re-run it periodically (or from CI) to keep the catalog current, since providers add and remove models frequently. The catalog is the authoritative source for context windows — note that a model's `:free` variant often has a different context length than the paid one.

- The Python bridge reads it via `MCP_BRIDGE_MODELS_CATALOG` (defaults to `models.json` in the repo root).
- `test_openrouter.sh` uses the live bridge `/models` endpoint and falls back to `models.json` if the bridge is unreachable.

### API Key Authentication

MCP-Bridge supports API key authentication to secure your server. To enable this feature, add something like this to your `config.json` file:

```json
{
    "security": {
      "auth": {
        "enabled": true,
        "api_keys": [
          {
            "key": "your-secure-api-key-here"
          }
        ]
      }
    }
}
```

When making requests to the MCP-Bridge server, include the API key in the Authorization header as a Bearer token:

```
Authorization: Bearer your-secure-api-key-here
```

If the `api_key` field is empty or not present in the configuration, authentication will be skipped, allowing backward compatibility.

### Full Configuration Example

an example config.json file with most of the options explicitly set:

```json
{
    "inference_server": {
        "base_url": "http://localhost:8000/v1",
        "api_key": "None"
    },
    "sampling": {
        "timeout": 10,
        "models": [
            {
                "model": "gpt-4o",
                "intelligence": 0.8,
                "cost": 0.9,
                "speed": 0.3
            },
            {
                "model": "gpt-4o-mini",
                "intelligence": 0.4,
                "cost": 0.1,
                "speed": 0.7
            }
        ]
    },
    "mcp_servers": {
        "fetch": {
            "command": "uvx",
            "args": [
                "mcp-server-fetch"
            ]
        }
    },
    "security": {
      "auth": {
        "enabled": true,
        "api_keys": [
          {
            "key": "your-secure-api-key-here"
          }
        ]
      }
    },
    "network": {
        "host": "0.0.0.0",
        "port": 9090
    },
    "logging": {
        "log_level": "DEBUG"
    }
}
```

| Section          | Description                        |
| ---------------- | ---------------------------------- |
| inference_server | The inference server configuration |
| mcp_servers      | The MCP servers configuration      |
| network          | uvicorn network configuration      |
| logging          | The logging configuration          |
| api_key          | API key for server authentication  |

## Support

If you encounter any issues please open an issue or join the [discord](https://discord.gg/4NVQHqNxSZ).

There is also documentation available [here](/docs/README.md).

## How does it work

The application sits between the OpenAI API and the inference engine. An incoming request is modified to include tool definitions for all MCP tools available on the MCP servers. The request is then forwarded to the inference engine, which uses the tool definitions to create tool calls. MCP bridge then manage the calls to the tools. The request is then modified to include the tool call results, and is returned to the inference engine again so the LLM can create a response. Finally, the response is returned to the OpenAI API.

```mermaid
sequenceDiagram
    participant OpenWebUI as Open Web UI
    participant MCPProxy as MCP Proxy
    participant MCPserver as MCP Server
    participant InferenceEngine as Inference Engine

    OpenWebUI ->> MCPProxy: Request
    MCPProxy ->> MCPserver: list tools
    MCPserver ->> MCPProxy: list of tools
    MCPProxy ->> InferenceEngine: Forward Request
    InferenceEngine ->> MCPProxy: Response
    MCPProxy ->> MCPserver: call tool
    MCPserver ->> MCPProxy: tool response
    MCPProxy ->> InferenceEngine: llm uses tool response
    InferenceEngine ->> MCPProxy: Response
    MCPProxy ->> OpenWebUI: Return Response
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

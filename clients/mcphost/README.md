# Usage

To use MCPhost with nao bridge you will need both MAP host and mcp-openapi-proxy

```bash
pip install mcp-openapi-proxy
go install github.com/mark3labs/mcphost@latest
```

Before starting MCPhost you need to set an environment variable to point at the nao-bridge swagger endpoint

```bash
export OPENAPI_SPEC_URL="http://localhost:3000/api/v1/swagger.json"
export TOOL_NAME_PREFIX="nao"
mcphost -m ollama:qwen2.5 --config config.json
```

# MCP Tool Server — ME-HAAT Fashion AI Bot v10.0

The **Model Context Protocol (MCP)** is an open standard that lets AI clients (Claude Desktop, IDEs, agent frameworks) discover and call external tools over a uniform JSON-RPC interface.

This server exposes ME-HAAT's internal agent tool registry (`agents/tools.py`) so any MCP client can search the catalogue, look up orders, generate reports, and more — the same capabilities the in-app multi-agent system uses.

## Endpoint

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/mcp` | MCP JSON-RPC 2.0 endpoint (single or batch requests) |
| `GET`  | `/mcp` | Human/JSON info page (server name, version, tool count) |
| `GET`  | `/mcp/tools` | Convenience JSON dump of the tool schemas |

Transport: **JSON-RPC 2.0 over HTTP**. Protocol version: **`2024-11-05`**.

The surface is gated by `MCP_ENABLED` (default `true`). When disabled, every route returns `404`.

## JSON-RPC methods

| Method | Params | Result |
| ------ | ------ | ------ |
| `initialize` | — | `{protocolVersion, serverInfo:{name,version}, capabilities:{tools:{}}}` |
| `tools/list` | — | `{tools: [...]}` — each tool has `name`, `description`, `inputSchema`, `annotations` |
| `tools/call` | `{name, arguments}` | `{content:[{type:"text", text}], isError}` |
| `ping` | — | `{}` |

Unknown methods return a JSON-RPC error `{code: -32601, message: "Method not found"}`. Malformed JSON returns `{code: -32700, message: "Parse error"}`. A handler error is reported as `{code: -32603}`, never an HTTP 500.

## Examples (curl)

### initialize
```bash
curl -s http://localhost:5000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```
```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2024-11-05",
  "serverInfo":{"name":"mehaat-fashion","version":"10.0"},
  "capabilities":{"tools":{}}}}
```

### tools/list
```bash
curl -s http://localhost:5000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```
```json
{"jsonrpc":"2.0","id":2,"result":{"tools":[
  {"name":"search_products",
   "description":"Search the store catalogue for products matching a query.",
   "inputSchema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]},
   "annotations":{"risk":"low","category":"sales"}}
]}}
```

### tools/call
```bash
curl -s http://localhost:5000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"search_products","arguments":{"query":"saree"}}}'
```
```json
{"jsonrpc":"2.0","id":3,"result":{
  "content":[{"type":"text","text":"[...]"}],
  "isError":false}}
```

## Connecting an MCP client

Point the client's server config at `POST /mcp`. Example (Claude Desktop / generic HTTP MCP config):

```json
{
  "mcpServers": {
    "mehaat-fashion": {
      "url": "https://your-host/mcp",
      "transport": "http",
      "headers": { "X-API-Key": "<your key>" }
    }
  }
}
```

## Approval-gated (high-risk) tools

High-risk tools (`issue_refund`, `send_broadcast`, `issue_coupon`) are routed through the human-approval workflow. Calling one via `tools/call` does **not** execute immediately; the result comes back with `isError: false` and a `text` payload explaining that the action has been **queued for admin approval** (including an `approval_id`). An operator must approve it in the admin console before it runs.

## Authentication

The `/mcp` routes are unauthenticated by default. In production, layer the existing API security in front of them — the same `X-API-Key` (or JWT bearer) accepted by the order/programmatic API. Configure `API_KEY` / `JWT_SECRET` and enforce it via a reverse proxy or a `before_request` guard on the blueprint so only trusted MCP clients can reach the tool endpoint.

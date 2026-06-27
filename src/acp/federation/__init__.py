"""Agent federation via MCP (Model Context Protocol) — v0.6.9.

Treats agents as microservices that expose their capabilities to one
another via MCP. An ACP task can discover and call tools exposed by
federated agents (other ACP instances, specialized micro-models, or
external services) without giving any agent direct network access.

Federation flow:
  1. ACP loads MCP server configs from the repo config (``federation``
     section).
  2. Before running the agent, ACP discovers available tools from each
     configured MCP server.
  3. The discovered tools are injected into the agent prompt so the
     agent knows what federated capabilities it can request.
  4. When the agent needs a federated capability, it emits a structured
     request in its output; ACP (the control plane) proxies the call to
     the MCP server — the agent never touches the network directly.

Security properties:
  - The agent never makes network calls. ACP proxies all MCP requests.
  - MCP servers are configured by the operator, not discovered at
    runtime (no rogue agents).
  - Every MCP tool call is recorded as a ``federation.tool_called``
    event in the hash-chained event log.
  - If an MCP server is unreachable, federation degrades gracefully —
    the run continues without that server's tools.
"""

# Dockerfile — placeholder for the MCP agent image (no real build yet).
# Uses uv for dependency install; serves the /mcp server.

FROM python:3.13-slim

WORKDIR /app

# TODO: install uv and project deps (from requirements.txt / pyproject)
# COPY pyproject.toml uv.lock ./
# RUN pip install uv && uv sync --frozen --no-dev

# TODO: copy source
# COPY api/ ./api/
# COPY mcp/ ./mcp/
# COPY agent.yaml ./

# TODO: run the MCP server
# CMD ["python", "-m", "mcp.server"]

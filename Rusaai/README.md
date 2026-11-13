# Rusaai — Control Plane boundary

Rusaai owns agent orchestration, task and workflow state, policy enforcement,
tool authorization, memory and retrieval coordination, audit, and the public
API boundary.

## Current implementation mapping

The current repository keeps the working implementation at the root for
backward compatibility:

- Agent contracts, workers, orchestrator, policy, queue, and workspace tools:
  rusa_agents/
- Knowledge ingestion and retrieval: rusa_knowledge/
- Read-only MCP integration: rusa_mcp/
- API and inference admission boundary: llm-project/serve/app.py
- Durable task, tenant, audit, and knowledge schemas: migrations/

This directory establishes the Rusaai ownership boundary. It intentionally
does not duplicate or shadow the existing Python packages. Move implementation
modules here only through a separately reviewed migration that preserves
imports, deployment paths, migrations, and tests.

The six AICode worker roles are not the ten-agent core runtime. The eight-stage
DevOps lifecycle remains a target until its dedicated agents, permissions,
state transitions, and approval APIs are implemented.

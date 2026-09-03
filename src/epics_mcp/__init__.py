"""epics-mcp -- a policy-gated MCP server for EPICS Channel Access.

Nothing here yet but the version. The implementation lands module by module
per PLAN.md 6 ("Phases"); this package currently exists so the repository
scaffolding, the packaging metadata and CI are reviewable before any code
that can touch a live instrument is written.

Planned modules (PLAN.md 5):
    policy.py      load/validate the YAML policy; match(name) -> Decision
    ca_client.py   Channel Access behind one small interface (pyepics, fake)
    audit.py       append-only JSON Lines log of every gated operation
    server.py      FastMCP tool surface (stdio and streamable-HTTP)
    cli.py         `epics-mcp --policy ...`
"""

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]

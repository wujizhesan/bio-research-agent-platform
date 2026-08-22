"""MCP server exposing the unified CADD and omics tool registry."""
import argparse
import json
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult, TextContent, Tool
except ImportError as exc:
    raise SystemExit('MCP server requires the optional dependency: pip install "mcp>=2.0"') from exc

try:
    from .domain_registry import run_tool, active_tool_specs
except ImportError:
    from domain_registry import run_tool, active_tool_specs


class BioMCPServer(MCPServer):
    def __init__(self):
        super().__init__(
            name='cadd-bio-agent',
            version='0.1.0',
            description='Cross-domain CADD and RNA-seq bioinformatics tools',
        )
        self._specs = {spec['name']: spec for spec in active_tool_specs()}

    async def list_tools(self):
        return [
            Tool(
                name=spec['name'],
                description=spec['description'],
                inputSchema=spec['parameters'],
            )
            for spec in self._specs.values()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        if name not in self._specs:
            result = {'status': 'error', 'error': f'unknown tool: {name}'}
        else:
            result = run_tool(name, arguments)
        is_error = isinstance(result, dict) and result.get('status') == 'error'
        return CallToolResult(
            content=[TextContent(text=json.dumps(result, ensure_ascii=False, default=str))],
            structuredContent=result,
            isError=is_error,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the CADD and omics MCP server')
    parser.add_argument('--list', action='store_true', help='list MCP tools and exit')
    args = parser.parse_args(argv)
    server = BioMCPServer()
    if args.list:
        print(json.dumps([
            {
                'name': spec['name'],
                'description': spec['description'],
                'inputSchema': spec['parameters'],
            }
            for spec in server._specs.values()
        ], ensure_ascii=False, indent=2))
        return
    server.run('stdio')


if __name__ == '__main__':
    main()

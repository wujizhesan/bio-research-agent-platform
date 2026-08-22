"""MCP server exposing the unified CADD and omics tool registry."""
import argparse
import json
from typing import Any

try:
    from mcp.types import CallToolResult, TextContent, Tool
    try:
        from mcp.server.mcpserver import MCPServer
        _MCP_API = 'legacy'
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        MCPServer = FastMCP
        _MCP_API = 'fastmcp'
except ImportError as exc:
    raise SystemExit('MCP server requires the optional dependency: pip install "mcp>=1.28"') from exc

if _MCP_API == 'fastmcp':
    class _CompatCallToolResult(CallToolResult):
        @property
        def is_error(self):
            return self.isError

        @property
        def structured_content(self):
            return self.structuredContent
else:
    _CompatCallToolResult = CallToolResult

try:
    from .domain_registry import run_tool, active_tool_specs
except ImportError:
    from domain_registry import run_tool, active_tool_specs


class BioMCPServer(MCPServer):
    def __init__(self):
        if _MCP_API == 'legacy':
            super().__init__(
                name='cadd-bio-agent',
                version='0.1.0',
                description='Cross-domain CADD and RNA-seq bioinformatics tools',
            )
        else:
            super().__init__(
                name='cadd-bio-agent',
                instructions='Cross-domain CADD and RNA-seq bioinformatics tools',
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
        return _CompatCallToolResult(
            content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False, default=str))],
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

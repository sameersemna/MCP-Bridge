import asyncio
from types import SimpleNamespace
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.openai_clients import utils as openai_utils

async def main():
    await ClientManager.initialize()
    print('CLIENTS', [name for name, _ in ClientManager.get_clients()])
    for name, client in ClientManager.get_clients():
        print('CLIENT', name, 'session_exists', bool(getattr(client, 'session', None)))
        if getattr(client, 'session', None):
            try:
                tools = await asyncio.wait_for(client.session.list_tools(), timeout=10)
                names = [getattr(t, 'name', None) for t in tools.tools]
                print('TOOLS', names)
            except Exception as exc:
                print('ERR', type(exc).__name__, exc)
    req = SimpleNamespace(messages=[SimpleNamespace(role='user', content='find github example')], tools=[])
    req = await openai_utils.chat_completion_add_tools(req)
    print('TOOL COUNT', len(req.tools))
    for tool in req.tools:
        if isinstance(tool, dict):
            fn = tool.get('function', {})
            print('TOOL', fn.get('name'))
        else:
            print('TOOL', getattr(tool, 'name', None))

asyncio.run(main())

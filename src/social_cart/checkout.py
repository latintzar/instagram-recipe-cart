"""Call the original Vitrina checkout_create_link MCP tool."""
import json
from urllib.parse import urlsplit
import httpx

async def create_checkout_link(mcp_url, bearer, allowed_origin, cart_ids, cart_key, thread_id):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def client_factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                 follow_redirects=False, trust_env=False)
    async with streamablehttp_client(mcp_url, headers={"Authorization":"Bearer " + bearer},
                                     httpx_client_factory=client_factory) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("checkout_create_link", arguments={
                "meta":{"api_version":"2026-04-17"}, "payload":{"cart_ids":cart_ids},
                "cart_key":cart_key, "thread_id":thread_id})
    if result.isError:
        raise RuntimeError("checkout link creation failed")
    data = getattr(result, "structuredContent", None)
    if not isinstance(data, dict):
        blocks = [block.text for block in result.content if getattr(block, "type", None) == "text"]
        data = json.loads(blocks[0]) if blocks else {}
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or len(url) > 450 or any(ord(c) < 32 for c in url):
        raise RuntimeError("invalid checkout link")
    parsed = urlsplit(url)
    origin = urlsplit(allowed_origin)
    if parsed.scheme != "https" or parsed.username or parsed.password or (parsed.scheme, parsed.hostname, parsed.port) != (origin.scheme, origin.hostname, origin.port):
        raise RuntimeError("checkout link is outside the configured origin")
    return url

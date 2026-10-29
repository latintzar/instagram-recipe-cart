import asyncio
import sys
import types
from contextlib import asynccontextmanager
import pytest
from social_cart.checkout import create_checkout_link

def install_fake_mcp(monkeypatch, checkout_url):
    calls = []
    @asynccontextmanager
    async def transport(url, **kwargs):
        calls.append(("transport",url,kwargs))
        # The pinned SDK receives an HTTP client with redirects disabled.
        client = kwargs["httpx_client_factory"](headers={}, timeout=5, auth=None)
        assert client.follow_redirects is False
        await client.aclose()
        yield (object(), object(), lambda:None)
    class Session:
        def __init__(self, read, write): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass
        async def call_tool(self, name, arguments):
            calls.append(("tool",name,arguments))
            return types.SimpleNamespace(isError=False, structuredContent={"url":checkout_url}, content=[])
    modules = {"mcp":types.ModuleType("mcp"), "mcp.client":types.ModuleType("mcp.client"),
               "mcp.client.streamable_http":types.ModuleType("mcp.client.streamable_http")}
    modules["mcp"].ClientSession = Session
    modules["mcp.client.streamable_http"].streamablehttp_client = transport
    for key,val in modules.items(): monkeypatch.setitem(sys.modules,key,val)
    return calls

def test_original_checkout_tool_and_own_origin(monkeypatch):
    calls = install_fake_mcp(monkeypatch, "https://grocery.example/checkout?s=synthetic")
    url = asyncio.run(create_checkout_link("https://grocery.example/mcp/","test-token",
                      "https://grocery.example",["cart-1"],"test-cart","test-thread"))
    assert url.endswith("?s=synthetic")
    assert calls[1][1] == "checkout_create_link"
    assert calls[1][2]["payload"]["cart_ids"] == ["cart-1"]

def test_checkout_rejects_foreign_link(monkeypatch):
    install_fake_mcp(monkeypatch, "https://attacker.example/checkout")
    with pytest.raises(RuntimeError, match="outside"):
        asyncio.run(create_checkout_link("https://grocery.example/mcp/","test-token",
                    "https://grocery.example",["cart-1"],"test-cart","test-thread"))

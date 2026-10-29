"""Exercise the pinned MCP client against a local synthetic Vitrina tool."""
import asyncio
import multiprocessing
import socket
import time
import pytest

pytest.importorskip("mcp")
from social_cart.checkout import create_checkout_link

def run_server(port):
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("synthetic-grocery", host="127.0.0.1", port=port)
    @server.tool()
    def checkout_create_link(meta: dict, payload: dict, cart_key: str, thread_id: str) -> dict:
        assert meta["api_version"] == "2026-04-17"
        assert payload["cart_ids"] == ["synthetic-cart"]
        return {"url":"https://grocery.example/checkout?s=synthetic"}
    server.run(transport="streamable-http")

def test_real_sdk_calls_synthetic_checkout_tool():
    sock = socket.socket()
    sock.bind(("127.0.0.1",0))
    port = sock.getsockname()[1]
    sock.close()
    process = multiprocessing.Process(target=run_server,args=(port,),daemon=True)
    process.start()
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1",port),timeout=0.1): break
            except OSError: time.sleep(0.1)
        else: pytest.fail("local synthetic MCP server did not start")
        url = asyncio.run(create_checkout_link(f"http://127.0.0.1:{port}/mcp/", "synthetic",
                         "https://grocery.example", ["synthetic-cart"],"synthetic-key","synthetic-thread"))
        assert url == "https://grocery.example/checkout?s=synthetic"
    finally:
        process.terminate()
        process.join(timeout=5)

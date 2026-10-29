import asyncio
import json
from social_cart import state, worker
from recipe_extractor import instagram_webhook, extract

def prepare(monkeypatch, tmp_path, fail=False):
    monkeypatch.setenv("CART_IDENTITY_SECRET", "a" * 40)
    monkeypatch.setenv("SOCIAL_CART_DB", str(tmp_path / "inbox.db"))
    monkeypatch.setenv("GROCERY_ORIGIN", "https://grocery.example")
    monkeypatch.setenv("VITRINA_MCP_URL", "https://grocery.example/mcp/")
    monkeypatch.setenv("VITRINA_MCP_BEARER_TOKEN", "synthetic")
    monkeypatch.setenv("CHECKOUT_ORIGIN", "https://grocery.example")
    monkeypatch.setattr(instagram_webhook, "iter_messaging_events", lambda payload: iter([{
        "sender_id":"123", "reel_url":"https://www.instagram.com/reel/ABC123/", "is_echo":False}]))
    monkeypatch.setattr(extract, "extract_recipe", lambda url, persist: {
        "status":"processed", "recipe":{"title":"Synthetic meal","servings":{"quantity":2},"ingredients":[]}})
    group = {"shop":{"id":"shop-1"},"items":[{"ingredient":"tomato","quantity_g":500,
              "product":{"id":"product-1","shop_id":"shop-1","unit_size":"500 g"}}]}
    monkeypatch.setattr(worker.shop_matcher, "match_recipe", lambda *a,**k: {"shop_groups":[group],"missing":[]})
    calls=[]
    class Grocery:
        def __init__(self, origin, key): pass
        def catalogue(self): return ([{"id":"shop-1"}],[{"id":"product-1","shop_id":"shop-1"}])
        def create_cart(self, shop): calls.append(("create", shop)); return "cart-1"
        def get_cart(self, cid): calls.append(("get", cid)); return {"status":"open"}
        def add_group(self, *args):
            calls.append(("add", args[0]))
            if fail: raise RuntimeError("ambiguous provider outcome")
        def close(self): pass
    monkeypatch.setattr(worker, "Grocery", Grocery)
    async def checkout(*args): calls.append(("checkout",args[3])); return "https://grocery.example/checkout?s=synthetic"
    monkeypatch.setattr(worker, "create_checkout_link", checkout)
    monkeypatch.setattr(instagram_webhook, "send_message", lambda recipient, text: calls.append(("send",recipient)) or {"message_id":"sent"})
    db = state.connect(str(tmp_path / "inbox.db"))
    return db,calls

def event(mid):
    return {"sender":{"id":"123"},"message":{"mid":mid,"text":"synthetic"}}

def test_worker_creates_then_reuses_the_same_shop_cart(monkeypatch,tmp_path):
    db,calls=prepare(monkeypatch,tmp_path)
    for mid in ("first","second"):
        assert state.enqueue(db,event(mid))
        asyncio.run(worker.process(db,*state.claim(db)))
        assert db.execute("SELECT status FROM jobs WHERE mid=?",(mid,)).fetchone()[0] == "done"
    assert calls.count(("create","shop-1")) == 1
    assert ("get","cart-1") in calls
    assert calls.count(("send","123")) == 2
    db.close()

def test_uncertain_cart_write_is_not_automatically_retried(monkeypatch,tmp_path):
    db,calls=prepare(monkeypatch,tmp_path,fail=True)
    assert state.enqueue(db,event("first"))
    asyncio.run(worker.process(db,*state.claim(db)))
    status,result = db.execute("SELECT status,result FROM jobs WHERE mid='first'").fetchone()
    assert status == "uncertain"
    assert json.loads(result)["stage"] == "cart"
    assert state.claim(db) is None
    assert not any(c[0] == "send" for c in calls)
    db.close()

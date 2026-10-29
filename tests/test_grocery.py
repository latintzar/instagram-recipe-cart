import httpx
from social_cart.grocery import Grocery
from social_cart.shop_matcher import _hydrate_match

def test_actual_vitrina_cart_contract_and_recipe_pack_quantities():
    calls = []
    def respond(request):
        calls.append(request)
        if request.method == "POST" and request.url.path == "/carts":
            return httpx.Response(200, json={"id":"cart-1","status":"open"})
        return httpx.Response(200, json={"id":"cart-1"})
    client = Grocery("https://grocery.example", "private-cart-key", transport=httpx.MockTransport(respond))
    group = {"shop":{"id":"shop-1"}, "items":[{"ingredient":"tomato","quantity_g":750,
        "product":{"id":"product-1","shop_id":"shop-1","unit_size":"500 g"}}]}
    cid = client.create_cart("shop-1")
    client.add_group(cid, group, "recipe-1", "Tomatoes", 2)
    assert [r.url.path for r in calls] == ["/carts", "/carts/cart-1/items"]
    import json
    payload = json.loads(calls[1].content)
    assert payload["quantity"] == 2
    assert payload["group_id"] == "rcp_recipe-1"
    assert calls[1].headers["X-Vitrina-Cart-Key"] == "private-cart-key"
    client.close()

def test_original_matcher_rejects_a_product_from_another_shop():
    result = _hydrate_match({"shop_groups":[{"shop_id":"shop-a","items":[{"product_id":"p-b"}]}]},
        shops_index={"shop-a":{"id":"shop-a"}}, products_index={"p-b":{"id":"p-b","shop_id":"shop-b"}},
        user_lat=None,user_lng=None,catchment_km=None,expanded_to_km=None,model_meta={})
    assert result["shop_groups"] == []

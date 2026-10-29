"""Vitrina's real stock and UCP cart REST contract, against an operator's server."""
from urllib.parse import quote
import httpx
from .policy import https_origin
from .quantities import _purchase_units_for

class Grocery:
    def __init__(self, origin, cart_key, transport=None):
        self.origin = https_origin(origin)
        self.cart_key = cart_key
        self.client = httpx.Client(timeout=20, follow_redirects=False, trust_env=False, transport=transport,
                                   headers={"X-Vitrina-Cart-Key": cart_key})

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        response = self.client.request(method, self.origin + path, **kwargs)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError("grocery request failed")
        return response.json()

    def catalogue(self):
        shops = self.request("GET", "/v1/shops").get("shops", [])
        if not isinstance(shops, list) or len(shops) > 100:
            raise ValueError("invalid shop catalogue")
        shaped, products = [], []
        for shop in shops:
            sid = str(shop["id"])
            card = dict(shop)
            card["address"] = {"lat":shop.get("lat"), "lng":shop.get("lng"),
                               "street_en":shop.get("address_en"), "street_bg":shop.get("address_bg")}
            shaped.append(card)
            data = self.request("GET", "/v1/shops/" + quote(sid, safe="") + "/products")
            for product in data.get("products", []):
                if str(product.get("shop_id")) == sid:
                    products.append(product)
        return shaped, products

    def get_cart(self, cart_id):
        return self.request("GET", "/carts/" + quote(cart_id, safe=""))

    def create_cart(self, shop_id):
        cart = self.request("POST", "/carts", json={"shop_id":str(shop_id), "currency":"EUR"})
        return str(cart["id"])

    def add_group(self, cid, group, recipe_id, title, servings):
        shop = group["shop"]
        gid = "rcp_" + recipe_id
        items = group.get("items") or []
        metadata = {"name":title, "serves":servings, "origin":"instagram",
                    "recipe_id":recipe_id, "lines":[], "substitutions":[]}
        for item in items:
            product = item["product"]
            quantity = _purchase_units_for(item, 1.0)
            metadata["lines"].append({"product_id":product["id"], "shop_id":str(shop["id"]),
                "ingredient":item.get("ingredient"), "quantity_recipe":item.get("quantity_recipe"),
                "quantity_purchase":item.get("quantity_purchase"), "quantity_g":item.get("quantity_g"),
                "pack_grams":item.get("pack_grams"), "unit_size":product.get("unit_size"),
                "unit":product.get("unit"), "units_needed":quantity, "substitution":item.get("substitution")})
        for line in metadata["lines"]:
            self.request("POST", "/carts/" + quote(cid, safe="") + "/items", json={
                "product_id":line["product_id"], "quantity":line["units_needed"],
                "group_id":gid, "group_metadata":metadata})

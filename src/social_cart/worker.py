"""Run the original reel → recipe → shop match → per-shop cart workflow."""
import argparse
import asyncio
import hashlib
import hmac
import logging
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
from .checkout import create_checkout_link
from .grocery import Grocery
from .policy import reel_url, https_origin
from .state import claim, connect, mark, cart_for, remember_cart
from . import shop_matcher

REQUIRED = ("SOCIAL_CART_DB", "INSTAGRAM_APP_SECRET", "INSTAGRAM_VERIFY_TOKEN", "INSTAGRAM_PAGE_ACCESS_TOKEN",
            "OPENROUTER_API_KEY", "GROCERY_ORIGIN", "CART_IDENTITY_SECRET",
            "VITRINA_MCP_URL", "VITRINA_MCP_BEARER_TOKEN", "CHECKOUT_ORIGIN")

def configuration():
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    token = os.environ["INSTAGRAM_PAGE_ACCESS_TOKEN"]
    if not token.startswith(("IGAA", "IGQ")) and not os.getenv("INSTAGRAM_BUSINESS_ID"):
        raise ValueError("INSTAGRAM_BUSINESS_ID is required for this token type")
    if len(os.environ["CART_IDENTITY_SECRET"]) < 32:
        raise ValueError("CART_IDENTITY_SECRET must contain at least 32 characters")
    https_origin(os.environ["GROCERY_ORIGIN"])
    https_origin(os.environ["CHECKOUT_ORIGIN"])
    mcp = urlsplit(os.environ["VITRINA_MCP_URL"])
    if mcp.scheme != "https" or not mcp.hostname or mcp.username or mcp.password or mcp.query or mcp.fragment or mcp.port not in (None,443):
        raise ValueError("VITRINA_MCP_URL must be an HTTPS URL")
    if bool(os.getenv("SERVICE_LAT")) != bool(os.getenv("SERVICE_LNG")):
        raise ValueError("Set both SERVICE_LAT and SERVICE_LNG")
    if os.getenv("SERVICE_LAT"):
        lat, lng = float(os.environ["SERVICE_LAT"]), float(os.environ["SERVICE_LNG"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("Invalid service location")

def private_cart_key(sender):
    secret = os.environ["CART_IDENTITY_SECRET"].encode()
    return hmac.new(secret, sender.encode(), hashlib.sha256).hexdigest()

async def process(db, mid, payload):
    os.environ.setdefault("RECIPE_OUTPUT_DIR", str(Path(os.environ["SOCIAL_CART_DB"]).expanduser().parent / "recipes"))
    for name in ("recipe_extractor.instagram_cdn", "recipe_extractor.instagram_webhook", "recipe_extractor"):
        logging.getLogger(name).disabled = True
    from recipe_extractor.instagram_webhook import iter_messaging_events, send_message
    from recipe_extractor.extract import extract_recipe

    uncertain = False
    grocery = None
    try:
        events = list(iter_messaging_events(payload))
        if len(events) != 1 or not events[0].get("reel_url") or events[0].get("is_echo"):
            raise ValueError("event has no supported reel")
        event = events[0]
        url = reel_url(event["reel_url"])
        extraction = extract_recipe(url, persist=False)
        if extraction.get("status") != "processed" or not isinstance(extraction.get("recipe"), dict):
            raise RuntimeError("recipe extraction failed")
        recipe = extraction["recipe"]
        sender = event["sender_id"]
        cart_key = private_cart_key(sender)
        grocery = Grocery(os.environ["GROCERY_ORIGIN"], cart_key)
        shops, products = grocery.catalogue()
        lat = float(os.environ["SERVICE_LAT"]) if os.getenv("SERVICE_LAT") else None
        lng = float(os.environ["SERVICE_LNG"]) if os.getenv("SERVICE_LNG") else None
        match = shop_matcher.match_recipe(recipe, shops=shops, products=products, user_lat=lat, user_lng=lng)
        groups = match.get("shop_groups") or []
        if not groups:
            raise RuntimeError("no purchasable matches")
        recipe_id = hashlib.sha256(mid.encode()).hexdigest()[:24]
        servings = (recipe.get("servings") or {}).get("quantity") or 4
        mark(db, mid, "uncertain", {"stage":"cart"})
        uncertain = True
        cart_ids = []
        for group in groups:
            shop_id = str(group["shop"]["id"])
            cart_id = cart_for(db, cart_key, shop_id)
            if cart_id and grocery.get_cart(cart_id).get("status") != "open":
                cart_id = None
            if not cart_id:
                cart_id = grocery.create_cart(shop_id)
                remember_cart(db, cart_key, shop_id, cart_id)
            grocery.add_group(cart_id, group, recipe_id, recipe.get("title") or "Recipe", servings)
            cart_ids.append(cart_id)
            mark(db, mid, "uncertain", {"stage":"cart", "cart_ids":cart_ids})
        namespace = uuid.UUID("6b3c8a2e-4e1b-4b2a-9a2b-0b1c2d3e4f5a")
        thread_id = str(uuid.uuid5(namespace, "instagram:" + sender))
        url = await create_checkout_link(os.environ["VITRINA_MCP_URL"], os.environ["VITRINA_MCP_BEARER_TOKEN"],
                                        os.environ["CHECKOUT_ORIGIN"], cart_ids, cart_key, thread_id)
        mark(db, mid, "uncertain", {"stage":"reply", "cart_ids":cart_ids})
        missing = [str(x.get("ingredient") or "")[:80] for x in
                   ((match.get("missing") or []) + (match.get("blockers") or [])) if isinstance(x, dict)]
        substitutions = []
        for group in groups:
            for item in group.get("items") or []:
                sub = item.get("substitution")
                if isinstance(sub, str) and sub.strip():
                    substitutions.append(str(item.get("ingredient") or "")[:50] + ": " + sub[:80])
        title = str(recipe.get("title") or "Recipe")[:100]
        message = title + ": your grocery cart is ready. " + url
        if missing:
            message += "\nItems to review: " + ", ".join(missing[:4])
        if substitutions:
            message += "\nSubstitutions: " + "; ".join(substitutions[:2])
        result = send_message(sender, message)
        if result.get("error") or not result.get("message_id"):
            raise RuntimeError("reply outcome needs review")
        mark(db, mid, "done", {"cart_ids":cart_ids})
    except Exception:
        # Avoid leaking provider response bodies, user data or tokens to logs.
        mark(db, mid, "uncertain" if uncertain else "failed")
    finally:
        if grocery:
            grocery.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="allow paid extraction, cart writes and replies")
    parser.add_argument("--once", action="store_true", help="process at most one queued message")
    parser.add_argument("--doctor", action="store_true", help="validate names and perform no network requests")
    parser.add_argument("--status", action="store_true", help="show queue counts without provider calls")
    args = parser.parse_args()
    if args.status:
        path = os.getenv("SOCIAL_CART_DB", "")
        if not path or not Path(path).expanduser().is_file():
            parser.error("Set SOCIAL_CART_DB to an existing private inbox.")
        with connect(path) as db:
            for status, count in db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status ORDER BY status"):
                print(f"{status}: {count}")
        return
    try:
        configuration()
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if args.doctor:
        print("Configuration present; no provider calls made.")
        return
    if not args.live:
        parser.error("Pass --live to process messages and call configured providers.")
    os.umask(0o077)
    with connect(os.environ["SOCIAL_CART_DB"]) as db:
        while True:
            job = claim(db)
            if job:
                asyncio.run(process(db, *job))
            if args.once:
                return
            time.sleep(2)

if __name__ == "__main__":
    main()

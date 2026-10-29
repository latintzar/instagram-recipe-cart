# Architecture

The original Vitrina agent accepted Instagram DMs and resolved Meta reel shares. Its social recipe extractor sampled video frames for a vision model; the shop matcher proposed products from the local catalog; `recipes.add_to_basket` grouped them into per-shop UCP carts and a checkout tool minted a basket-review link.

This derivative keeps those boundaries and the original extractor, Meta share-resolution code, matcher and pack calculations. A signed FastAPI webhook writes a durable SQLite inbox. An operator-started worker processes each message against the operator's Vitrina-compatible catalog and cart API, calls its checkout MCP tool, then replies through Meta. The matcher hydrates only known products and checks that each product belongs to the selected shop.

Per-sender cart identities are HMAC-derived from an operator secret. The private database records the sender's webhook event and per-shop cart IDs so future recipes can reuse an open cart. New work is marked uncertain before external writes. A crash or ambiguous request is not automatically retried.

The original code's Sofia demo shops and customer records are not included. The service uses only catalog responses from the configured grocery server. Instagram is the interaction surface.

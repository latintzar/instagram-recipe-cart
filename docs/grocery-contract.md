# Grocery service contract

This project targets the actual Vitrina UCP/ACP implementation recovered from the original source.

The configured HTTPS REST origin supplies:

- `GET /v1/shops` → an object with `shops`; each shop has an ID, names and optional latitude/longitude.
- `GET /v1/shops/{shop_id}/products` → an object with `products`, each with its product ID, shop ID, unit size, price and stock.
- `POST /carts` → creates a cart for `shop_id` and `currency`.
- `GET /carts/{cart_id}` → returns a cart and its status.
- `POST /carts/{cart_id}/items` → adds a product ID, sellable-unit quantity, recipe group ID and group metadata.

Cart calls use `X-Vitrina-Cart-Key` for the anonymous shopper identity. The separate authenticated streamable MCP service exposes the original `checkout_create_link` tool, taking `meta.api_version = 2026-04-17`, cart IDs, cart key and thread ID. The returned HTTPS URL must match `CHECKOUT_ORIGIN`.

Your backend must enforce shopper isolation, catalog ownership and authentication. The historical alpha REST route decoded JWTs without verification; it needs a verified authentication boundary before deployment. [Reference source](../reference/vitrina/README.md) is provided for review, not as a commissioned production grocery service.

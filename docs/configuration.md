# Configuration

Supply values privately through your process environment or service manager. Do not put keys or account data in the repository. `instagram-recipe-cart-worker --doctor` checks that values are present and URLs have the expected shape; it does not authenticate them or make network calls.

| Variable | Purpose |
| --- | --- |
| `SOCIAL_CART_DB` | Path to the private SQLite inbox and cart mapping |
| `INSTAGRAM_APP_SECRET` | Meta webhook HMAC secret |
| `INSTAGRAM_VERIFY_TOKEN` | Chosen Meta webhook handshake token |
| `INSTAGRAM_PAGE_ACCESS_TOKEN` | Your Instagram Messaging API token |
| `INSTAGRAM_BUSINESS_ID` | Your Instagram business ID when required by the token type |
| `OPENROUTER_API_KEY` | Your vision extraction and shop-matching provider key |
| `GROCERY_ORIGIN` | HTTPS origin of your Vitrina-compatible grocery REST service |
| `CART_IDENTITY_SECRET` | Private random secret of at least 32 characters for per-sender cart identities |
| `VITRINA_MCP_URL` | HTTPS streamable MCP URL, typically ending in `/mcp/` |
| `VITRINA_MCP_BEARER_TOKEN` | Your MCP service bearer token |
| `CHECKOUT_ORIGIN` | HTTPS origin allowed in the returned basket-review link |
| `SERVICE_LAT`, `SERVICE_LNG` | Optional service-area coordinates, supplied together |
| `RECIPE_OUTPUT_DIR` | Optional private output directory; defaults beside the inbox database |

The original matcher uses a 1.5 km catchment with a 2.5 km expansion on partial matches when a location is supplied. Without coordinates it matches the configured catalog's entire shop set. The default extractor and matcher models are configurable through `RECIPE_MODEL` and `VITRINA_MATCHER_MODEL`.

Keep `CART_IDENTITY_SECRET` stable: changing it creates new cart identities. `instagram-recipe-cart-worker --status` shows private inbox counts without provider calls.

The worker runs only with `--live`. It can call Meta, OpenRouter and the configured grocery service and can mutate carts or send Instagram replies. Keep its network and filesystem access limited to the configured services and private storage.

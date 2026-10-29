---
name: instagram-recipe-cart
description: Set up or diagnose an Instagram reel-to-local-grocery-cart service.
---

# Instagram Recipe Cart

Read the README and docs before acting. Use the operator's own Meta, model and grocery accounts. Keep secrets in a private environment or secret store, never in chat or command arguments. Run the offline tests and `instagram-recipe-cart-worker --doctor`; distinguish those checks from live provider authentication.

The webhook only queues verified messages. Running the worker with `--live` can incur provider costs, create carts and send Instagram replies, so do that only on the operator's explicit instruction. Never automatically retry an uncertain cart write or reply; inspect the private state and grocery service with the operator.

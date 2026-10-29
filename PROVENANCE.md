# Provenance

- Vitrina monorepo snapshot `dbac69422281b857b9425ee01bb1876ddbdb7c2c`: original shop matcher, unit parser, recipe purchase-size calculations, Instagram/commerce reference files and schema. The portable matcher accepts the operator's live catalog instead of bundled Sofia seed data, filters by catchment and rejects product IDs assigned to the wrong shop.
- [Social Recipes Kit](https://github.com/latintzar/social-recipes-kit) commit `84f5f6615e3d5650e0cba04db20122dc0aab52db`: original video extractor, prompts and Instagram share resolution. Its optional webhook helper is fail-closed when its secret is absent, message IDs are URL-encoded, and the Meta CDN fetch does not follow redirects.
- New glue: authenticated webhook queue, private cart identity and checkpointing, Vitrina-compatible REST/MCP clients and worker orchestration. Tests use synthetic records and mocked providers.

The original operating service, customer records, shop seed data, account credentials and private Git history are excluded.

# Vitrina commerce source

These are retained source references from the original Vitrina Instagram recipe-to-cart system: its Instagram event and reel adapters, UCP cart persistence, Supabase transport, and commerce/checkout schemas. The running service uses the portable implementation in `src/`; the reference files are not executed by it and retain their original dependencies.

Review and adapt the historical migrations and identity/authentication model before deploying a Vitrina-compatible backend. The original REST cart route decoded JWTs without verifying their signature; do not expose that historic trust model. Customer records, catalog data, credentials and operating endpoints are not included. The blank historical alert-hook seed is omitted.

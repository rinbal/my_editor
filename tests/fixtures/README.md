# Test fixtures

- `standup_editorial_fixtures.json`: the shared test vectors of STANDUP's editorial contract v1, copied unchanged from `docs/editorial-fixtures.json` in EINUNDZWANZIG STANDUP (branch `feat/redaktion`, file at commit 569b3f8; contract `docs/editorial-contract.md` v1 at commit 569b3f8). Regenerate there with `docs/editorial-fixtures/generate.py`, never edit here.
- `publication_check_cases.json`: the Publication Check's shared cases, and `nostr/publication_check.json` its rules, copied unchanged from EINUNDZWANZIG STANDUP (`tests/fixtures/publication_check_cases.json` and `shared/publication-check.json`, branch `feat/publication-check`, both files at commit 577e7d6e). `tests/test_readiness.py` pins both by SHA-256: change them there and copy them again, never edit here.
- `media-preview.mp4`: a short clip for the media preview tests.

# Changelog

## [0.2.0](https://github.com/Brennan-VanderLaan/lazyaf/compare/v0.1.0...v0.2.0) (2026-09-01)


### Features

* close 12.6.5, 12.6.6 and 12.7; harden the API surface ([db5f9f5](https://github.com/Brennan-VanderLaan/lazyaf/commit/db5f9f5cca39e957dcd3a1784f94f430be0457b1))
* **endpoints:** detect image and audio support, and never claim what was not proven ([70f9d6c](https://github.com/Brennan-VanderLaan/lazyaf/commit/70f9d6c6c6d4a0d912912bdcdff5a355e3dbdf0e))
* **endpoints:** self-hosted OpenAI-compatible model endpoints (M14) ([4b429c6](https://github.com/Brennan-VanderLaan/lazyaf/commit/4b429c6ef9e4ef2a422589713279d9cd6b4d4588))
* **pipelines:** retire the v1 array format from the wire and the executor (12.8 P3-P5) ([96ed87d](https://github.com/Brennan-VanderLaan/lazyaf/commit/96ed87d9fcd86643cc2d98b7ae90f6d965395a69))
* **pipelines:** the graph gains terminal actions (12.8 P1-P2) ([b79bb7f](https://github.com/Brennan-VanderLaan/lazyaf/commit/b79bb7fb25398dfc3575d4e92daa95501990a7f1))
* **workspaces:** one workspace per WORKER, not per run (M13-1) ([08e356d](https://github.com/Brennan-VanderLaan/lazyaf/commit/08e356d5fde3e90b2955f502416bf4f473dbcd69))


### Bug Fixes

* **cards:** refuse to start work when the default branch does not exist ([aae20fa](https://github.com/Brennan-VanderLaan/lazyaf/commit/aae20fad3f592ef5fd315e525667a0ace3e53c14))
* **ci:** a gate that cannot tell a passing suite from a suite that never ran ([c5b6668](https://github.com/Brennan-VanderLaan/lazyaf/commit/c5b666814ab685d827b434c9b5ae8825883a4a4f))
* commit migration 0007, which 0009 has been referencing from main ([ecdeb0c](https://github.com/Brennan-VanderLaan/lazyaf/commit/ecdeb0c629a4f1495cb018713d1ffe82d903f36f))
* **test-mode:** make /api/test/seed idempotent, and stop the QA lane lying ([5334b09](https://github.com/Brennan-VanderLaan/lazyaf/commit/5334b0980a32d4d305ad6fd930af0e885617294e))
* **ui:** connection resilience, live-update correctness, and demo polish ([a39cb24](https://github.com/Brennan-VanderLaan/lazyaf/commit/a39cb2462dca7e9d9ae61c150c05d7d12cfd0088))
* **ui:** playground history, scroll and selection; plus the newcomer path ([744376b](https://github.com/Brennan-VanderLaan/lazyaf/commit/744376b7cd0be141fad22eebd3bdfefa798a0657))


### Documentation

* **m13:** corpus format, solution-graph leaderboards, and the review that says not to publish them yet ([4f529e1](https://github.com/Brennan-VanderLaan/lazyaf/commit/4f529e172e0223c0657b1bd9b0486ef58d5e8aa5))
* reconcile PLAN.md's status tables with the tree ([7d611fd](https://github.com/Brennan-VanderLaan/lazyaf/commit/7d611fdd22750432dfae9455cea0fc68f4b13551))
* reconcile the written record with the tree, and add a per-commit workflow catalog ([b54dd19](https://github.com/Brennan-VanderLaan/lazyaf/commit/b54dd19ed34721a94295ee0baea13bab12b45479))
* rewrite the README for people who have never seen this ([1243619](https://github.com/Brennan-VanderLaan/lazyaf/commit/1243619b3245def718a25dcf12c07ab7e37275a0))
* shadow CI design, and the reason it must not reuse the push trigger ([bca5b0a](https://github.com/Brennan-VanderLaan/lazyaf/commit/bca5b0a19ecbdf977ac4cc8f15f50f982c7d0286))
* wave design docs, QA findings, and the v1 retirement plan ([a55c49e](https://github.com/Brennan-VanderLaan/lazyaf/commit/a55c49e3dc4fdccb4a064b72a441c3790d45a5ec))

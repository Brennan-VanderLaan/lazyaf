# Changelog

## [0.2.0](https://github.com/Brennan-VanderLaan/lazyaf/compare/v0.1.0...v0.2.0) (2026-09-16)


### Features

* close 12.6.5, 12.6.6 and 12.7; harden the API surface ([c8542c6](https://github.com/Brennan-VanderLaan/lazyaf/commit/c8542c60cca1d0f92f9c2d7e346fe64f07d14eec))
* **diagnostics:** a logs view and a redacted bug-report bundle ([f85d06d](https://github.com/Brennan-VanderLaan/lazyaf/commit/f85d06d4190b2ca4cdbd7773059c9e2c979422be))
* **endpoints:** detect image and audio support, and never claim what was not proven ([b106377](https://github.com/Brennan-VanderLaan/lazyaf/commit/b106377b356ebe0a06f0548fe7dc6ea2146722a7))
* **endpoints:** self-hosted OpenAI-compatible model endpoints (M14) ([42f2892](https://github.com/Brennan-VanderLaan/lazyaf/commit/42f289290980aafd87e0933a3eb3a2456bf8d825))
* **pipelines:** drop pipelines.steps, and land the parked QoL lanes (12.8 P6) ([d9a6c0b](https://github.com/Brennan-VanderLaan/lazyaf/commit/d9a6c0b85bc426f7d211778d8a3f806b0b564f53))
* **pipelines:** retire the v1 array format from the wire and the executor (12.8 P3-P5) ([dae2eb6](https://github.com/Brennan-VanderLaan/lazyaf/commit/dae2eb661bda73467b9e0befd7a98d33ba279ea4))
* **pipelines:** the graph gains terminal actions (12.8 P1-P2) ([e5716e3](https://github.com/Brennan-VanderLaan/lazyaf/commit/e5716e3928b77097cb5250071f83029dcf404c67))
* **workspaces:** one workspace per WORKER, not per run (M13-1) ([62003a0](https://github.com/Brennan-VanderLaan/lazyaf/commit/62003a015f9fafe17750730fe57be984b7ac1ffa))


### Bug Fixes

* **cards:** refuse to start work when the default branch does not exist ([77ae1b1](https://github.com/Brennan-VanderLaan/lazyaf/commit/77ae1b18aadf4e45a1b37b4fd57eac386611753d))
* **ci:** a gate that cannot tell a passing suite from a suite that never ran ([35563eb](https://github.com/Brennan-VanderLaan/lazyaf/commit/35563ebf7b46a0294b33de1f46c706492d0e2914))
* commit migration 0007, which 0009 has been referencing from main ([a682dc3](https://github.com/Brennan-VanderLaan/lazyaf/commit/a682dc387d5bac2b2918adeeec87e6b3393219e4))
* **experiments:** an experiment could report 100% over zero measurements ([6c1bab7](https://github.com/Brennan-VanderLaan/lazyaf/commit/6c1bab7f1b430c4a7f4e4ed13ee81e87b2f9f40e))
* **repos:** adopt a real default branch at push time, not whenever someone looks ([38688db](https://github.com/Brennan-VanderLaan/lazyaf/commit/38688db7cac3c66228559a849b836c06fc08f486))
* **test-mode:** make /api/test/seed idempotent, and stop the QA lane lying ([feaa7ff](https://github.com/Brennan-VanderLaan/lazyaf/commit/feaa7ff9885bef6737dc09269d894ea6e2d67124))
* **ui:** connection resilience, live-update correctness, and demo polish ([ace58f8](https://github.com/Brennan-VanderLaan/lazyaf/commit/ace58f898241e5832a8db5030c119397246a1de7))
* **ui:** playground history, scroll and selection; plus the newcomer path ([319676f](https://github.com/Brennan-VanderLaan/lazyaf/commit/319676fc3cf465c732ecdeb867baeb4afcd2d001))


### Documentation

* **m13:** corpus format, solution-graph leaderboards, and the review that says not to publish them yet ([e143b05](https://github.com/Brennan-VanderLaan/lazyaf/commit/e143b055e20a46abfb49c9da6f26fa9c604bf6ec))
* reconcile PLAN.md's status tables with the tree ([c87bb34](https://github.com/Brennan-VanderLaan/lazyaf/commit/c87bb34f6c35b98e0bf84c2aa94af30970c99525))
* reconcile the written record with the tree, and add a per-commit workflow catalog ([47b379e](https://github.com/Brennan-VanderLaan/lazyaf/commit/47b379ecc5b0e2e50595b25004d9898fd4bef305))
* rewrite the README for people who have never seen this ([2acbe85](https://github.com/Brennan-VanderLaan/lazyaf/commit/2acbe85c2200f92308941b8e298bd3f630297e86))
* shadow CI design, and the reason it must not reuse the push trigger ([1045481](https://github.com/Brennan-VanderLaan/lazyaf/commit/104548152f18efb0ce8b112ad00dd97d260794b7))
* wave design docs, QA findings, and the v1 retirement plan ([934ae5f](https://github.com/Brennan-VanderLaan/lazyaf/commit/934ae5fd5a6d7dfbeb80fe4750d464ff070b8995))

"""`python -m tdd.shared.mock_openai` - the compose `mock-endpoint` service.

Serves every scenario at once under its own URL prefix, so ONE process backs
both dogfood endpoints (`dogfood-mock` -> /happy_tools/v1,
`dogfood-mock-notools` -> /happy_text/v1) and every T2 test.
"""
import sys

from .scenarios import SCENARIOS
from .server import build_server


def main(argv: list[str] | None = None) -> int:
    server = build_server(argv)
    server.start()
    print(
        f"[mock-openai] listening on :{server.port} - scenarios: "
        f"{', '.join(sorted(SCENARIOS))}",
        flush=True,
    )
    try:
        while True:
            # serve_forever runs on the server's own daemon thread; block here
            # so the container's PID 1 stays alive and SIGTERM still lands.
            import time

            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

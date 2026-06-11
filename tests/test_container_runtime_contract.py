from __future__ import annotations

import re
from pathlib import Path


def test_backend_container_defaults_to_at_least_four_web_workers() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile.read_text()

    env_match = re.search(r"WEB_CONCURRENCY=(\d+)", content)
    assert env_match is not None
    assert int(env_match.group(1)) >= 4

    fallback_match = re.search(r"--workers\s+\$\{WEB_CONCURRENCY:-(\d+)\}", content)
    assert fallback_match is not None
    assert int(fallback_match.group(1)) >= 4

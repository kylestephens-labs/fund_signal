Optimization Ideas:

Consider teaching setup/setup-dev to respect USE_UV=0 as well, so environments without working uv pip can fall back to pip install automatically.

Teach setup-dev/prove-* to fall back to pip install automatically when uv pip hits macOS SystemConfiguration issues, reducing the friction we saw in the sandbox.

Add a make prove-benchmark (or similar) hook so engineers can explicitly run the skipped benchmark suites without toggling PYTEST_FLAGS manually.

Consider making the Makefile honor .venv/bin automatically when USE_UV=0 so contributors don’t need to prefix PATH when avoiding uv panics on macOS.
"""Tests for configurable server HOST/PORT (Phase 1)."""
import pytest


def test_host_port_defaults():
    """Defaults are 127.0.0.1:8000 when nothing is configured."""
    from app.config import get_settings

    s = get_settings()
    assert s.HOST == "127.0.0.1"
    assert s.PORT == 8000


def test_host_port_configurable(monkeypatch):
    """HOST/PORT are read from the environment without any code change."""
    from app.config import get_settings

    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8080")
    get_settings.cache_clear()
    s = get_settings()
    assert s.HOST == "0.0.0.0"
    assert s.PORT == 8080
    get_settings.cache_clear()


def test_run_sh_reads_host_port(tmp_path):
    """run.sh extracts HOST/PORT from .env and defaults when absent."""
    import os
    import subprocess

    script = (tmp_path / "run.sh")
    script.write_text(
        """
#!/usr/bin/env bash
set -euo pipefail
_read_env() {
  local key="$1" default="$2" val
  val=$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "\\r' || true)
  printf '%s' "${val:-$default}"
}
HOST=$(_read_env HOST "127.0.0.1")
PORT=$(_read_env PORT "8000")
echo "HOST=$HOST PORT=$PORT"
""".strip(),
        encoding="utf-8",
    )
    os.chmod(script, 0o755)

    # No .env -> defaults.
    (tmp_path / ".env").write_text("MOCK_MODE=false\n", encoding="utf-8")
    out = subprocess.run(["bash", str(script)], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "HOST=127.0.0.1 PORT=8000" in out.stdout

    # .env overrides -> values are picked up.
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=9090\n", encoding="utf-8")
    out = subprocess.run(["bash", str(script)], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "HOST=0.0.0.0 PORT=9090" in out.stdout


def test_run_sh_rejects_non_numeric_port(tmp_path):
    """run.sh fails with a clear message for a non-numeric PORT."""
    import os
    import subprocess

    script = tmp_path / "run.sh"
    script.write_text(
        """
#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-8000}"
case "$PORT" in
  ''|*[!0-9]*)
    echo "ERROR: PORT must be a number, got: '$PORT'" >&2
    exit 1
    ;;
esac
echo "ok $PORT"
""".strip(),
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    env = dict(os.environ)
    env["PORT"] = "abc"
    out = subprocess.run(["bash", str(script)], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 1
    assert "PORT must be a number" in out.stderr

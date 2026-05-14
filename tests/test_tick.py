import station.tick as tick


def test_env_float_uses_default_for_missing_env(monkeypatch):
    monkeypatch.delenv("DRIFT_TICK_INTERVAL_SECONDS", raising=False)
    assert tick._env_float("DRIFT_TICK_INTERVAL_SECONDS", 60.0) == 60.0


def test_env_float_reads_override(monkeypatch):
    monkeypatch.setenv("DRIFT_TICK_INTERVAL_SECONDS", "0.25")
    assert tick._env_float("DRIFT_TICK_INTERVAL_SECONDS", 60.0) == 0.25


def test_env_float_ignores_invalid_override(monkeypatch):
    monkeypatch.setenv("DRIFT_TICK_INTERVAL_SECONDS", "fast")
    assert tick._env_float("DRIFT_TICK_INTERVAL_SECONDS", 60.0) == 60.0

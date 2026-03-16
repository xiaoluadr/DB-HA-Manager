import json
from datetime import datetime, timedelta, timezone
from threading import Lock

from app.drivers.oracle import OracleDataGuardDriver


def test_get_sync_history_filters_by_time_range(tmp_path):
    driver = OracleDataGuardDriver.__new__(OracleDataGuardDriver)
    driver.cluster_id = "test-cluster"
    driver._sync_history_dir = tmp_path / "sync_history"
    driver._sync_history_dir.mkdir(parents=True, exist_ok=True)
    driver._history_lock = Lock()

    now = datetime.now(timezone.utc)
    history_file = driver._history_file_path(driver.cluster_id)

    samples = [
        {"timestamp": (now - timedelta(minutes=10)).isoformat(), "lag_seconds": 1.2, "status": "SYNCED"},
        {"timestamp": (now - timedelta(minutes=5)).isoformat(), "lag_seconds": 4.5, "status": "LAGGING"},
        {"timestamp": (now - timedelta(minutes=1)).isoformat(), "lag_seconds": 0.2, "status": "SYNCED"},
    ]

    history_file.write_text(json.dumps(samples), encoding="utf-8")

    start = now - timedelta(minutes=6)
    end = now - timedelta(minutes=2)

    history = driver.get_sync_history(start_time=start, end_time=end)

    assert len(history) == 1
    assert history[0]["status"] == "LAGGING"
    assert history[0]["lag_seconds"] == samples[1]["lag_seconds"]

import socket
from typing import Dict, Any

from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from app.api.oracle_adg import OraclePreviewExecutor, DEFAULT_SSH_CONNECT_TIMEOUT
from app.main import app
from app.models.schemas import PreviewRequest, DiscoveryInfo, PlanStage


def _build_preview_payload() -> Dict[str, Any]:
    """提供一个最小可用的 PreviewRequest 负载"""
    return {
        "primary_host": "10.0.0.10",
        "primary_ssh_port": 22,
        "primary_ssh_user": "oracle",
        "primary_ssh_auth_type": "password",
        "primary_ssh_password": "Passw0rd!",
        "standby_host": "10.0.0.11",
        "standby_ssh_port": 22,
        "standby_ssh_user": "oracle",
        "standby_ssh_auth_type": "password",
        "standby_ssh_password": "Passw0rd!",
        "oracle_sid": "ORCLCDB",
        "oracle_home": "/u01/app/oracle/product/19c/dbhome_1",
        "db_name": "ORCLCDB",
        "db_unique_name_primary": "ORCLCDB_PRIM",
        "db_unique_name_standby": "ORCLCDB_STBY",
        "data_files_path": "/u01/oradata/ORCLCDB",
        "archivelog_path": "/u01/oradata/ORCLCDB/archivelog",
    }


def test_demo_preview_returns_mock_payload():
    client = TestClient(app)
    payload = _build_preview_payload()
    response = client.post("/api/oracle/adg/preview?demo=true", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["is_demo"] is True
    assert body["data"]["discovered_info"]["primary_oracle"]["db_unique_name"].endswith("_PRIM")


def test_preview_returns_structured_error_for_unreachable_host(monkeypatch):
    payload = _build_preview_payload()

    class FailingExecutor:
        def __init__(self, config):
            self.config = config
            self.last_error = None

        def connect(self):
            self.last_error = socket.timeout("timed out")
            return False

        def close(self):
            pass

    monkeypatch.setattr("app.api.oracle_adg.RemoteExecutor", FailingExecutor)

    client = TestClient(app)
    response = client.post("/api/oracle/adg/preview", json=payload)

    assert response.status_code == 504
    body = response.json()
    detail = body["detail"]
    assert detail["error_type"] == "host_unreachable"
    assert detail["host"] == payload["primary_host"]


def test_preview_returns_structured_error_for_generic_ssh_failure(monkeypatch):
    payload = _build_preview_payload()

    class GenericFailingExecutor:
        def __init__(self, config):
            self.config = config
            self.last_error = None

        def connect(self):
            self.last_error = RuntimeError("handshake failed")
            return False

        def close(self):
            pass

    monkeypatch.setattr("app.api.oracle_adg.RemoteExecutor", GenericFailingExecutor)

    client = TestClient(app)
    response = client.post("/api/oracle/adg/preview", json=payload)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error_type"] == "ssh_connection_failed"
    assert detail.get("details", {}).get("error") == "handshake failed"


def test_preview_returns_structured_error_for_unexpected_exception(monkeypatch):
    payload = _build_preview_payload()

    class ExplodingExecutor:
        def __init__(self, config):
            self.config = config

        def connect(self):
            raise RuntimeError("boom")

        def close(self):
            pass

    monkeypatch.setattr("app.api.oracle_adg.RemoteExecutor", ExplodingExecutor)

    client = TestClient(app)
    response = client.post("/api/oracle/adg/preview", json=payload)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error_type"] == "preview_error"
    assert "RuntimeError" in detail["message"]


def test_preview_executor_builds_ssh_config_with_short_timeout():
    request = PreviewRequest(**_build_preview_payload())
    executor = OraclePreviewExecutor(request)

    ssh_config = executor._build_primary_ssh_config()

    assert ssh_config.connect_timeout == DEFAULT_SSH_CONNECT_TIMEOUT


def test_generate_plan_returns_plan_stages_and_serializes():
    request = PreviewRequest(**_build_preview_payload())
    executor = OraclePreviewExecutor(request)

    plan = executor.generate_plan(DiscoveryInfo(), [])

    assert plan.stages, "generate_plan should produce at least one stage"
    assert all(isinstance(stage, PlanStage) for stage in plan.stages)

    encoded = jsonable_encoder(plan)
    assert isinstance(encoded["stages"], list)
    assert all("stage_name" in stage for stage in encoded["stages"])


def test_generate_plan_sanitizes_tuple_metadata(monkeypatch):
    request = PreviewRequest(**_build_preview_payload())
    executor = OraclePreviewExecutor(request)

    original_model_dump = OraclePreviewExecutor._model_dump

    def injecting_model_dump(self, model):
        payload = original_model_dump(self, model)
        stage_name = payload.get("stage_name")
        if stage_name == "prepare_primary":
            payload["metadata"] = {
                ("primary_path", "standby_path"): ("/u01/oradata/prim", "/u02/oradata/std"),
            }
        return payload

    monkeypatch.setattr(OraclePreviewExecutor, "_model_dump", injecting_model_dump)

    plan = executor.generate_plan(DiscoveryInfo(), [])
    encoded = jsonable_encoder(plan)

    metadata = encoded["stages"][0]["metadata"]
    assert metadata == {"primary_path -> standby_path": ["/u01/oradata/prim", "/u02/oradata/std"]}

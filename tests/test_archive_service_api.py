import importlib.util
from pathlib import Path


def test_archive_api_keeps_scheduler_and_archive_health_separate(tmp_path, monkeypatch):
    from trading_team import archive_launchd, archive_service

    spec = importlib.util.spec_from_file_location("archive_console_test", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    observed = []
    def service_status(**kwargs):
        observed.append(kwargs)
        return {"status": "healthy", "heartbeat": "fresh", "expected_session": "2026-09-21"}
    def launchd_status(**kwargs):
        observed.append(kwargs)
        return {"status": "not_loaded", "loaded": False}
    monkeypatch.setattr(archive_service, "service_status", service_status)
    monkeypatch.setattr(archive_launchd, "launchd_status", launchd_status)
    handler = object.__new__(module.H)
    handler.path = "/api/archive-service?refresh=1"
    sent = []
    handler._send_json = lambda status, payload: sent.append((status, payload))
    handler.do_GET()
    assert observed == [{"team": tmp_path / "trading_team"}, {"root": tmp_path}]
    assert sent == [(200, {"ok": True, "status": "healthy", "heartbeat": "fresh",
                          "expected_session": "2026-09-21", "scheduler": {"status": "not_loaded", "loaded": False}})]
    assert not list(tmp_path.iterdir())

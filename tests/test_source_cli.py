import json

from crypto_event_harvester.cli import build_parser, main


def test_source_defaults():
    args = build_parser().parse_args(["sources"])
    assert args.source == "all"
    assert args.engine == "http"
    assert args.strict is False


def test_source_cli_generates_report(monkeypatch, capsys, tmp_path):
    captured = []

    def fake_run(names, **kwargs):
        captured.extend(names)
        return {"sources": [{"status": "listing_evidence"}]}

    monkeypatch.setattr("crypto_event_harvester.source_feasibility.probe_sources", fake_run)
    path = tmp_path / "report.json"
    assert main(["sources", "--source", "coindar", "--json-out", str(path)]) == 0
    assert captured == ["coindar"]
    assert json.loads(path.read_text()) == json.loads(capsys.readouterr().out)


def test_source_strict_fails_when_no_evidence(monkeypatch):
    monkeypatch.setattr(
        "crypto_event_harvester.source_feasibility.probe_sources",
        lambda names, **kwargs: {"sources": [{"status": "blocked"}]},
    )
    assert main(["sources", "--strict"]) == 1

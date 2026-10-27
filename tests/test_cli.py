import json

from crypto_event_harvester.cli import build_parser, main
from crypto_event_harvester.probe import PageSnapshot


def test_cli_defaults():
    args = build_parser().parse_args(["probe", "--url", "https://example.test"])
    assert args.engine == "http"
    assert args.timeout == 30
    assert args.save_html is None


def test_cli_prints_diagnostics_and_saves_html(tmp_path, monkeypatch, capsys):
    html = "<title>Events</title><div class='event'></div>"
    monkeypatch.setattr(
        "crypto_event_harvester.cli.fetch_page",
        lambda *args, **kwargs: PageSnapshot(
            "https://example.test", "https://example.test", "http", 200, html
        ),
    )
    path = tmp_path / "pages" / "one.html"
    status = main(
        ["probe", "--url", "https://example.test", "--selector", ".event", "--save-html", str(path)]
    )
    assert status == 0
    assert path.read_text() == html
    assert json.loads(capsys.readouterr().out)["selector_matches"] == 1


def test_cli_handles_fetch_errors(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr("crypto_event_harvester.cli.fetch_page", fail)
    assert main(["probe", "--url", "https://example.test"]) == 2
    assert "unavailable" in capsys.readouterr().err

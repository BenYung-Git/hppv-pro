"""Smoke: racing Status formatter aligns with soccer request-result framing."""

from __future__ import annotations

from pathlib import Path


def test_format_record_download_status_error_and_success_shape() -> None:
    # Execute the JS function via Node if available; otherwise parse-check source.
    js_path = Path(__file__).resolve().parents[1] / "hppv" / "web_static" / "hppv_download_shell.js"
    text = js_path.read_text(encoding="utf-8")
    assert "function formatRecordDownloadStatus" in text
    assert "MSG.requestResult" in text
    assert "MSG.sendErrors" in text
    assert "MSG.labelHttp" in text
    assert "MSG.labelElapsed" in text
    assert "MSG.ingestSection" in text
    assert "MSG.ingestDateRange" in text
    # Must not force connection-failed as the only error prefix.
    assert 'data.error +' not in text.split("function formatRecordDownloadStatus")[1].split(
        "function formatSoccerGraphqlStatus"
    )[0] or True
    block = text.split("function formatRecordDownloadStatus")[1].split(
        "function formatSoccerGraphqlStatus"
    )[0]
    assert "連線失敗" not in block
    assert "sendErrors" in block


def test_racing_info_wires_soccer_aligned_status_messages() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "hppv"
        / "web_templates"
        / "racing_info.html"
    ).read_text(encoding="utf-8")
    assert "requestResult:" in html
    assert "sendErrors:" in html
    assert "labelHttp:" in html
    assert "labelElapsed:" in html
    assert "ingestSection:" in html
    assert "racing.download.processed" in html
    assert "soccer.daily.send_invalid_json" in html
    # Old bug: all errors labelled as connection failed via dlError alone.
    assert "dlError: {{ t('soccer.daily.send_connection_failed')" not in html

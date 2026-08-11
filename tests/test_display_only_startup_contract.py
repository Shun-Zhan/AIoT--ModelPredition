"""Static contracts for the display-only desktop startup path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_startup_scripts_do_not_configure_desktop_cloud_or_models() -> None:
    for relative_path in (
        "scripts/start_dashboard.sh",
        "scripts/start_dashboard.ps1",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "cloud-check" not in text
        assert "cloud-configure" not in text
        assert "VEI API Key" not in text
        assert "artifacts/" not in text


def test_readmes_document_esp32_as_the_production_authority() -> None:
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    firmware_readme = (
        ROOT / "firmware/esp32_s3_all_sensors/README.md"
    ).read_text(encoding="utf-8")

    for text in (root_readme, firmware_readme):
        assert "GPIO5 / GPIO8" in text
        assert "NTP" in text
        assert "ArduinoJson 7.4.2" in text
        assert "V1" in text
        assert "V2" in text

    assert "电脑或手机网页只是显示数据和转发用户操作" in root_readme
    assert "TTL UART" in root_readme
    assert "不是 RS485 电气层" in root_readme

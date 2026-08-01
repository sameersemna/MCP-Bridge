from pathlib import Path


def test_genpdf_uses_weasyprint():
    script_path = Path(__file__).resolve().parents[1] / "genpdf.sh"
    script_text = script_path.read_text(encoding="utf-8")

    assert "weasyprint" in script_text
    assert "wkhtmltopdf" not in script_text

from pathlib import Path
import re
import xml.etree.ElementTree as ET


README = Path(__file__).parents[1] / "README.md"


def test_readme_has_no_control_characters():
    text = README.read_text(encoding="utf-8")
    invalid = [char for char in text if ord(char) < 32 and char not in {"\n", "\r"}]
    assert invalid == []


def test_readme_math_blocks_are_complete():
    text = README.read_text(encoding="utf-8")
    assert text.count("$$") >= 6
    assert text.count("$$") % 2 == 0
    assert "Score_i = \\frac{M_i}{\\max(\\sigma_{40,i}, 0.10)^{0.75}}" in text
    assert "w_i = \\frac{1 / \\sigma_i}{\\sum_j (1 / \\sigma_j)}" in text
    # Catch the original malformed formula without rejecting a valid ``\frac``.
    assert " = rac{" not in text


def test_readme_visuals_and_navigation_exist():
    text = README.read_text(encoding="utf-8")
    root = README.parent
    expected = [
        "docs/assets/etf-rotation-logo.svg",
        "docs/assets/quant-engine-hero.svg",
        "docs/assets/quant-system-architecture.svg",
        "docs/assets/alpha-portfolio-engine.svg",
        "docs/QUICKSTART.md",
        "docs/OPERATIONS.md",
        "docs/LLM.md",
        "docs/PAGES.md",
    ]
    for relative in expected:
        assert relative in text
        assert (root / relative).is_file()


def test_readme_svg_assets_are_accessible_and_parseable():
    text = README.read_text(encoding="utf-8")
    references = re.findall(r"!\[([^]]*)\]\((docs/assets/[^)]+\.svg)\)", text)
    assert references
    assert all(alt.strip() for alt, _ in references)
    assets = [README.parent / relative for _, relative in references]
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    for asset in assets:
        assert asset.is_file()
        root = ET.parse(asset).getroot()
        assert root.get("role") == "img"
        assert root.get("viewBox")
        assert root.find("svg:title", namespace) is not None
        assert root.find("svg:desc", namespace) is not None

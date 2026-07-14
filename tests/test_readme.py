from pathlib import Path


README = Path(__file__).parents[1] / "README.md"


def test_readme_has_no_control_characters():
    text = README.read_text(encoding="utf-8")
    invalid = [char for char in text if ord(char) < 32 and char not in {"\n", "\r"}]
    assert invalid == []


def test_readme_math_blocks_are_complete():
    text = README.read_text(encoding="utf-8")
    assert text.count("```math") == 3
    assert "Score_i = \\frac{M_i}{\\max(\\sigma_{40,i}, 0.10)^{0.75}}" in text
    assert "w_i = \\frac{1 / \\sigma_i}{\\sum_j (1 / \\sigma_j)}" in text
    # Catch the original malformed formula without rejecting a valid ``\frac``.
    assert " = rac{" not in text

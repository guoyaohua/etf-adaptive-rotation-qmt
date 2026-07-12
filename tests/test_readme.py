from pathlib import Path


README = Path(__file__).parents[1] / "README.md"


def test_readme_has_no_control_characters():
    text = README.read_text(encoding="utf-8")
    invalid = [char for char in text if ord(char) < 32 and char not in {"\n", "\r"}]
    assert invalid == []


def test_readme_math_blocks_are_complete():
    text = README.read_text(encoding="utf-8")
    assert text.count("```math") == 3
    assert "Score_i = M_i / max(σ_{40,i}, 0.10)" in text
    assert "w_i = (1 / σ_i) / Σ_j (1 / σ_j)" in text
    assert "rac{" not in text

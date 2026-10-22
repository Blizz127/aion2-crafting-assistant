from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aion2_crafting as crafting


def test_default_config_is_bounded():
    config = crafting.CraftConfig()
    config.validate()
    assert config.max_clicks == 10
    assert config.interval == 12.0


@pytest.mark.parametrize(
    "config",
    [
        crafting.CraftConfig(interval=2.9),
        crafting.CraftConfig(interval=300.1),
        crafting.CraftConfig(max_clicks=0),
        crafting.CraftConfig(max_clicks=1000),
    ],
)
def test_config_rejects_unbounded_values(config):
    with pytest.raises(ValueError):
        config.validate()


def test_button_match_accepts_small_visual_drift_only():
    reference = bytes([100] * 120)
    assert crafting.button_matches(reference, bytes([109] * 120))
    assert not crafting.button_matches(reference, bytes([140] * 120))
    assert not crafting.button_matches(reference, bytes([100] * 119))


def test_cursor_location_parser_reads_xdotool_shell_output():
    assert crafting.parse_cursor_location("X=123\nY=456\nSCREEN=0\n") == (123, 456)


def test_window_geometry_parser_reads_xwininfo_output():
    output = """
      Absolute upper-left X:  10
      Absolute upper-left Y:  20
      Width: 1920
      Height: 1080
    """
    assert crafting.parse_window_geometry(output) == (10, 20, 1920, 1080)

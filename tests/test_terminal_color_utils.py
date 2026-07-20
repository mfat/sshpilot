"""Pure color math extracted from TerminalWidget (sshpilot/terminal_color_utils.py).

The normal suite runs against a stubbed ``gi`` (no real ``Gdk.RGBA``), so we inject
a minimal fake ``Gdk`` whose ``RGBA`` stores float channels and parses ``#RRGGBB``.
Only that storage/parse is faked — the blend, luminance, clone and contrast logic
under test is real Python.
"""
import pytest

from sshpilot import terminal_color_utils as cu


class _RGBA:
    def __init__(self):
        self.red = self.green = self.blue = self.alpha = 0.0

    def parse(self, spec):
        s = spec.lstrip("#")
        self.red = int(s[0:2], 16) / 255
        self.green = int(s[2:4], 16) / 255
        self.blue = int(s[4:6], 16) / 255
        self.alpha = 1.0
        return True


class _FakeGdk:
    RGBA = _RGBA


@pytest.fixture(autouse=True)
def _fake_gdk(monkeypatch):
    monkeypatch.setattr(cu, "Gdk", _FakeGdk)


def _rgba(spec):
    c = _RGBA()
    c.parse(spec)
    return c


def test_get_contrast_color_dark_on_light():
    c = cu.get_contrast_color(_rgba("#FFFFFF"))
    assert (c.red, c.green, c.blue) == pytest.approx((27 / 255, 27 / 255, 29 / 255))  # #1B1B1D
    assert c.alpha == 1.0


def test_get_contrast_color_light_on_dark():
    c = cu.get_contrast_color(_rgba("#000000"))
    assert (c.red, c.green, c.blue, c.alpha) == (1.0, 1.0, 1.0, 1.0)


def test_relative_luminance_light_gt_dark():
    assert cu.relative_luminance(_rgba("#FFFFFF")) > cu.relative_luminance(_rgba("#000000"))
    assert cu.relative_luminance(_rgba("#000000")) == pytest.approx(0.0, abs=1e-9)
    assert cu.relative_luminance(_rgba("#FFFFFF")) == pytest.approx(1.0, abs=1e-9)


def test_clone_rgba_is_independent_copy():
    src = _rgba("#123456")
    dup = cu.clone_rgba(src)
    assert (dup.red, dup.green, dup.blue, dup.alpha) == (src.red, src.green, src.blue, src.alpha)
    dup.red = 0.999  # mutating the clone must not touch the source
    assert src.red != dup.red


def test_mix_rgba_endpoints_and_midpoint():
    black, white = _rgba("#000000"), _rgba("#FFFFFF")
    assert cu.mix_rgba(black, white, 0.0).red == pytest.approx(0.0)
    assert cu.mix_rgba(black, white, 1.0).red == pytest.approx(1.0)
    assert cu.mix_rgba(black, white, 0.5).red == pytest.approx(0.5)
    # ratio clamps outside 0..1
    assert cu.mix_rgba(black, white, 2.0).red == pytest.approx(1.0)
    assert cu.mix_rgba(black, white, -1.0).red == pytest.approx(0.0)

"""Colour tokens in app.css: the status chips, heat cells, buttons and the dark sidebar stay readable in both themes."""
import re
from pathlib import Path

from django.test import SimpleTestCase

CSS = (Path(__file__).resolve().parent / "static" / "linkedeye" / "app.css").read_text()
MIN = 4.5


def block(start):
    i = CSS.index(start)
    return CSS[CSS.index("{", i) + 1:CSS.index("\n}", i)]


def tokens(text):
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", text))


def parse(value, env):
    value = value.strip()
    m = re.match(r"var\((--[\w-]+)\)", value)
    if m:
        return parse(env[m.group(1)], env)
    if value.startswith("#"):
        h = value[1:]
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.match(r"rgba?\(([^)]*)\)", value)
    parts = [float(x) for x in m.group(1).split(",")]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def over(fg, bg):
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4
    return .2126 * ch(c[0]) + .7152 * ch(c[1]) + .0722 * ch(c[2])


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + .05) / (lb + .05)


class ContrastTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        dark = tokens(block(":root {"))
        light = dict(dark, **tokens(block('html[data-theme="light"] {')))
        cls.themes = {"dark": dark, "light": light}

    def pair(self, env, fg, bg, under="--panel"):
        base = parse(env[under], env)
        back = over(parse(env[bg], env), base)
        return ratio(over(parse(env[fg], env), back), back)

    def test_status_chips_and_badges_are_readable(self):
        for theme, env in self.themes.items():
            for tone in ("ok", "warn", "crit", "info", "purple", "teal", "mute"):
                r = self.pair(env, f"--{tone}-ink", f"--{tone}-bg")
                self.assertGreaterEqual(r, MIN, (theme, tone, round(r, 2)))

    def test_heat_cells_and_solid_buttons_are_readable(self):
        for theme, env in self.themes.items():
            for tone in ("ok", "warn", "crit", "unk"):
                r = self.pair(env, f"--heat-{tone}-ink", f"--heat-{tone}")
                self.assertGreaterEqual(r, MIN, (theme, tone, round(r, 2)))
            for fg, bg in (("--btn-primary-ink", "--btn-primary"), ("--active-ink", "--active-fill")):
                r = self.pair(env, fg, bg)
                self.assertGreaterEqual(r, MIN, (theme, fg, round(r, 2)))

    def test_body_text_on_the_page_and_cards(self):
        for theme, env in self.themes.items():
            for fg in ("--ink", "--ink-2", "--muted"):
                for bg in ("--bg", "--panel", "--panel-2"):
                    r = self.pair(env, fg, bg, under="--bg")
                    self.assertGreaterEqual(r, MIN, (theme, fg, bg, round(r, 2)))

    def test_links_are_readable_on_cards(self):
        for theme, env in self.themes.items():
            r = self.pair(env, "--primary", "--panel", under="--bg")
            self.assertGreaterEqual(r, MIN, (theme, round(r, 2)))

    def test_the_sidebar_is_readable_in_both_themes(self):
        # the rail is dark in both themes; light redefines the tokens for it under `html[data-theme=light] .side`
        for theme, env in self.themes.items():
            env = dict(env)
            if theme == "light":
                env.update(tokens(block('html[data-theme="light"] .side {')))
                env["--side-ink"], env["--side-muted"], env["--side"] = env["--side-rail-ink"], env["--side-rail-muted"], env["--side-rail"]
            for fg in ("--side-ink", "--side-muted"):
                r = self.pair(env, fg, "--side", under="--side")
                self.assertGreaterEqual(r, MIN, (theme, fg, round(r, 2)))
            r = self.pair(env, "--crit-ink", "--crit-bg", under="--side")      # the sidebar badge
            self.assertGreaterEqual(r, MIN, (theme, "badge", round(r, 2)))

"""Every template in every installed app must compile, and no template tag may be split across lines.

Django's lexer matches `{% ... %}`, `{{ ... }}` and `{# ... #}` one line at a time. An editor that reflows HTML therefore
breaks templates in two ways, and neither shows up until the page is rendered:

* a split `{% if %}` / `{% endif %}` / `{% for %}` is a TemplateSyntaxError, so every page that extends the template is a 500;
* a split `{{ variable }}` is not an error at all: the raw text is printed into the page.

Nothing else in the suite renders every template (many are only reached by a particular role, client or data), so this
is the one place that catches both before they ship.
"""
import os
import re

from django.apps import apps
from django.template import engines
from django.template.base import Origin, Template
from django.template.exceptions import TemplateSyntaxError
from django.test import SimpleTestCase

TEMPLATE_SUFFIXES = (".html", ".txt")
# a tag that opens and does not close on the same line
SPLIT_TAG = re.compile(r"\{%(?!.*%\})|\{\{(?!.*\}\})|\{#(?!.*#\})")


def template_files():
    """[(app label, name the loader knows it by, path)] for every template shipped by an installed app."""
    found = []
    for cfg in apps.get_app_configs():
        root = os.path.join(cfg.path, "templates")
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(TEMPLATE_SUFFIXES):
                    path = os.path.join(dirpath, name)
                    found.append((cfg.label, os.path.relpath(path, root), path))
    return sorted(found)


class TemplateIntegrityTests(SimpleTestCase):
    def test_templates_are_found(self):
        # guards the guard: an empty list would make the two tests below pass without checking anything
        self.assertGreater(len(template_files()), 100)

    def test_every_template_compiles(self):
        # compile each FILE's source, not the name: get_template(name) returns the first app's copy, so a template
        # shadowed by a same-named one in an earlier app would never be checked. The origin carries the template's name,
        # so a relative {% extends "./x.html" %} or {% include %} resolves as the loader resolves it.
        engine = engines["django"].engine
        for label, name, path in template_files():
            with self.subTest(template=f"{label}:{name}"):
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                try:
                    Template(source, origin=Origin(path, template_name=name), name=name, engine=engine)
                except TemplateSyntaxError as exc:
                    self.fail(f"{label}:{name} does not compile: {exc}")

    def test_relative_template_names_compile(self):
        # a relative name needs the template's own name to resolve against (from_string has none and refuses it)
        engine = engines["django"].engine
        source = '{% extends "./base.html" %}{% block content %}{% include "../core/_pager.html" %}{% endblock %}'
        Template(source, origin=Origin("page.html", template_name="core/page.html"), name="core/page.html", engine=engine)

    def test_no_tag_is_split_across_lines(self):
        for label, name, path in template_files():
            with self.subTest(template=f"{label}:{name}"):
                with open(path, encoding="utf-8") as fh:
                    broken = [f"line {n}: {line.strip()[:90]}" for n, line in enumerate(fh, 1) if SPLIT_TAG.search(line)]
                self.assertEqual(broken, [], f"{label}:{name} has template tags wrapped across lines; "
                                             "keep each {% %}, {{ }} and {# #} on one line")

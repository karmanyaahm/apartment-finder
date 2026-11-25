"""app.js is one script: a second `function NAME(` silently replaces the first, so names must be unique."""
import collections
import pathlib
import re

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "src/aptfinder/web/static/app.js"


def test_app_js_has_no_duplicate_function_declarations():
    names = re.findall(r"^\s*(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", APP_JS.read_text(), re.M)
    dupes = sorted(n for n, c in collections.Counter(names).items() if c > 1)
    assert names and not dupes, f"declared more than once in app.js: {dupes}"

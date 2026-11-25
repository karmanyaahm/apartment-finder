"""Every command-line entry point imports (the test suite otherwise never loads some of them)."""

import importlib
import pkgutil

import aptfinder


def test_every_cli_module_imports():
    names = [m.name for m in pkgutil.walk_packages(aptfinder.__path__, "aptfinder.") if m.name.endswith("cli")]
    assert "aptfinder.intake.cli" in names
    for n in names:
        importlib.import_module(n)

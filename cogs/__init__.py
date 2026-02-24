"""Deadlock bot cogs package.

Extends the package search path so subpackages provided from external
repositories (e.g. Deadlock-Steam-Bot) can be imported as ``cogs.*``.
"""
from pkgutil import extend_path

# Allow multiple `cogs` directories on sys.path to behave like one namespace.
__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]

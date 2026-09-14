"""The replay engine: executes a saved capability artifact and reports what happened.

Like `cua/artifact/`, this package is pure -- no browser driver import, no DOM handle,
no CSS selector or XPath. `tests/test_architecture.py` greps the whole package for that.
"""

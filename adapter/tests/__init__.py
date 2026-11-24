"""Test package for the unified source adapter.

Two invocation styles are supported from ``tools/source_adapter`` without any
install step:

* ``python3 -m unittest discover -s tests -v`` (the acceptance command) imports
  the modules as top-level names and puts ``tests/`` on ``sys.path``.
* ``python3 -m unittest tests.test_models -v`` imports the ``tests`` package and
  puts the adapter root on ``sys.path``.

The package itself therefore sits next to ``tests/`` instead of under ``src/``,
and test modules import their helpers through a small try/except shim.
"""

# Atlas tests package
#
# Pytest collection: 0 errors expected (verified 2026-05-08, task #261).
# Collection is stable because:
#   - All test subdirectories (brokers/, monitor/, overlay/, services/, ui/) have __init__.py
#   - pytest.ini uses --import-mode=importlib to avoid top-level package collisions
#   - norecursedirs excludes tests/archive, __pycache__, *.egg-info
#
# If you see "ERROR collecting ..." during CI, check:
#   1. New test subdirectory missing __init__.py
#   2. Import in a test file refers to a module that does not exist
#   3. Syntax error in a newly added test file (run: python3 -m py_compile <file>)

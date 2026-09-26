"""nh hooks: stdlib-only (Python >= 3.9) handlers run by ``hooks/nh-hook`` through ``main.py``.

``main.py`` puts this directory and ``server/src`` on ``sys.path``, so the handlers import
each other as top-level modules and the shared code as ``nh_gateway._shared``.
"""

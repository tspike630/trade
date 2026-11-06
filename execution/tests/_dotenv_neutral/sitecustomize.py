"""No-op startup hook for execution tests that must see the real dotenv loader.

Python imports the first ``sitecustomize`` on ``sys.path``. A project or
virtualenv hook earlier than this file can call ``load_dotenv`` on the
repository ``.env`` before ``crawler/config.py`` runs. Putting this directory
first skips that hook and does not read a ``.env``. It does not replace
``dotenv``; ``crawler/config.py`` still loads ``crawler/.env`` itself.
Production processes do not put this directory on ``PYTHONPATH``.
"""

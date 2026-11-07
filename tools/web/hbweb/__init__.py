"""The local web server's package: `router.py` maps each request to a feature module.

`config.py` holds every file path and setting the routes read (read as `config.X` at call
time, so a test that moves a file moves it for every route); `basis.py` the request parsers
and the account bases; `jobs.py` the background searches and their event streams. The
feature modules are `search`, `songs`, `board`, `boardplan`, `event`, `gacha`, `park`,
`collection`, `inventory`, `admin` and `static` (pages and assets).
"""

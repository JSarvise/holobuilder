"""`GET /api/version`: the app's version, the one source `src/hbuilder/VERSION`."""

from __future__ import annotations

from hbweb import router

from hbuilder import paths


class _Handler:
    def __init__(self) -> None:
        self.replies: list = []

    def reply_json(self, status, doc) -> None:
        self.replies.append((status, doc))


def test_the_version_route_answers_the_version_file() -> None:
    h = _Handler()
    router.serve_version(h)  # type: ignore[arg-type]
    want = (paths.REPO / "src" / "hbuilder" / "VERSION").read_text(encoding="utf-8").strip()
    assert h.replies == [(200, {"version": want})]
    assert router.GET_ROUTES["/api/version"] is router.serve_version

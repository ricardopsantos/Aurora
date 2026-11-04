"""R404: notes keep a user order (drag or Alt+arrows on the page); the server stores it for every device."""
import json

import pytest

from tests.test_web import app, call  # noqa: F401  (fixture)


def _ids(app, q=""):
    return [n["id"] for n in call(app, "/api/notes" + (f"?q={q}" if q else ""))[1]["notes"]]


def _mk(app, *titles):
    return [call(app, "/api/notes/create", {"title": t, "text": t.lower()})[1]["id"] for t in titles]


def test_r404_new_notes_go_on_top_and_an_edit_does_not_move_a_note(app):
    a, b, c = _mk(app, "A", "B", "C")
    assert _ids(app) == [c, b, a]
    assert call(app, "/api/notes/save", {"id": a, "title": "A", "text": "changed"})[0] == 200
    assert _ids(app) == [c, b, a]                              # the user's order wins over "recently edited"


def test_r404_reorder_all_and_a_subset_shown_by_a_search(app):
    a, b, c, d = _mk(app, "Apple", "Bread", "Apricot", "Dates")   # list: d c b a
    assert call(app, "/api/notes/order", {"ids": [a, b, c, d]})[0] == 200
    assert _ids(app) == [a, b, c, d]
    assert _ids(app, "ap") == [a, c]
    assert call(app, "/api/notes/order", {"ids": [c, a]})[0] == 200   # the two swap places, b and d stay put
    assert _ids(app) == [c, b, a, d]


def test_r404_reorder_does_not_touch_updated_so_no_save_conflict(app):
    a, b = _mk(app, "A", "B")
    base = call(app, f"/api/notes/{a}")[1]["updated"]
    call(app, "/api/notes/order", {"ids": [a, b]})
    assert call(app, "/api/notes/save", {"id": a, "title": "A", "text": "x", "base": base})[0] == 200


@pytest.mark.parametrize("ids,code", [(None, 400), ([], 400), ("abc", 400), (["../x"], 400), (["0123456789ab"], 404), ("dup", 400)])
def test_r404_bad_orders_are_refused_and_change_nothing(app, ids, code):
    a, b = _mk(app, "A", "B")
    if ids == "dup":
        ids = [a, a]
    assert call(app, "/api/notes/order", {"ids": ids})[0] == code
    assert _ids(app) == [b, a]


def test_r404_notes_from_before_r404_keep_their_newest_first_order(app):
    import os
    a, b, c = _mk(app, "A", "B", "C")
    p = os.path.join(os.environ["AURORA_HOME"], "web-notes", "index.json")
    idx = json.load(open(p))
    for k in idx:
        idx[k].pop("pos")
    idx[a]["updated"] = 9e9                                    # most recently edited
    json.dump(idx, open(p, "w"))
    assert _ids(app) == [a, c, b]
    assert all("pos" in v for v in json.load(open(p)).values())   # written once, then stable

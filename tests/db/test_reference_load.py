from decimal import Decimal

import pytest

from refinery_watch.db.connect import Conn
from refinery_watch.extract.reference import load_refineries, load_source

pytestmark = pytest.mark.integration


def test_refineries_come_back_with_units_tags_and_capacities(db: Conn) -> None:
    refs = {r.id: r for r in load_refineries(db)}
    assert set(refs) == {"R9001", "R9002", "R9003"}
    testville = refs["R9001"]
    assert testville.name == "Testville Refinery"
    assert testville.status == "operating"
    assert testville.nameplate_kbd == Decimal("200.0")
    assert testville.owner == "Test Oil Co"
    assert sorted(testville.units) == ["CDU", "FCC", "VDU"]
    cdu = testville.units["CDU"]
    assert len(cdu) == 1
    assert cdu[0].unit_tag == ""
    assert cdu[0].capacity_kbd == Decimal("200.0")
    assert testville.units["FCC"][0].capacity_kbd is None


def test_aliases_status_and_missing_owner(db: Conn) -> None:
    refs = {r.id: r for r in load_refineries(db)}
    assert refs["R9003"].aliases == ("Samplebay", "Sample Bay Works")
    assert refs["R9003"].owner is None
    assert refs["R9002"].status == "closed_or_converted"
    assert refs["R9001"].aliases == ()


def test_refinery_without_units_has_an_empty_map(db: Conn) -> None:
    db.execute(
        "INSERT INTO refinery (id, name, country, region, status)"
        " VALUES ('R9999', 'No Units Plant', 'Testland', 'Other', 'operating')"
    )
    refs = {r.id: r for r in load_refineries(db)}
    assert refs["R9999"].units == {}


def test_trains_give_more_than_one_unit_row(db: Conn) -> None:
    db.execute(
        "INSERT INTO refinery_unit (refinery_id, unit_code, unit_tag, capacity_kbd)"
        " VALUES ('R9001', 'CDU', 'T2', 50)"
    )
    cdu = {r.id: r for r in load_refineries(db)}["R9001"].units["CDU"]
    assert [u.unit_tag for u in cdu] == ["", "T2"]


def test_load_source(db: Conn) -> None:
    ref = load_source(db, "hp_news")
    assert (ref.id, ref.tier) == ("hp_news", 2)
    with pytest.raises(LookupError, match="nope"):
        load_source(db, "nope")


def test_the_app_role_can_read_the_reference(app_db: Conn) -> None:
    assert len(load_refineries(app_db)) >= 3

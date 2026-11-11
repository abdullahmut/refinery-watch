"""Edges of the any-chain restart window. These pass today."""

import pytest
from tests.db.support import link, outage, restart

from refinery_watch.db.connect import Conn
from refinery_watch.db.events import Duplicate, Written, write_event

pytestmark = pytest.mark.integration


def test_retrip_day_after_same_day_restart_is_new(db: Conn) -> None:
    e1 = write_event(db, outage(db, 5, 5), link(db, 1))
    assert isinstance(e1, Written)
    x1 = write_event(db, restart(db, 5, 6), link(db, 2))
    assert isinstance(x1, Written)
    assert x1.ends_event_id == e1.id
    retrip = write_event(db, outage(db, 6, 7), link(db, 3))
    assert isinstance(retrip, Written), retrip


def test_edge_retrip_on_the_restart_day_of_the_previous_chain_still_merges(db: Conn) -> None:
    write_event(db, outage(db, 1, 1), link(db, 10))
    write_event(db, restart(db, 5, 5), link(db, 11))
    second = write_event(db, outage(db, 5, 6), link(db, 12))
    assert isinstance(second, Written), second
    result = write_event(db, outage(db, 7, 7), link(db, 13))
    assert isinstance(result, Duplicate), result
    assert result.chain_id == second.chain_id


def test_edge_late_report_before_a_same_day_retrip_goes_to_its_own_chain(db: Conn) -> None:
    first = write_event(db, outage(db, 1, 1), link(db, 60))
    assert isinstance(first, Written)
    write_event(db, restart(db, 3, 3), link(db, 61))
    second = write_event(db, outage(db, 3, 4), link(db, 62))
    assert isinstance(second, Written), second
    result = write_event(db, outage(db, 1, 5), link(db, 63))
    assert isinstance(result, Duplicate), result
    assert result.chain_id == first.chain_id

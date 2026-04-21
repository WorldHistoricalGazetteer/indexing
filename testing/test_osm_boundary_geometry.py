#!/usr/bin/env python3
"""Lightweight regression checks for shared OSM/OHM boundary geometry helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from processing.osm_boundary_geometry import (
    build_timespans,
    is_admin_boundary_value,
    is_misc_boundary_value,
    parse_year,
    process_relation_tags,
)


class FakeTag:
    def __init__(self, k, v):
        self.k = k
        self.v = v


class FakeTags:
    def __init__(self, mapping):
        self._mapping = dict(mapping)
        self._tags = [FakeTag(k, v) for k, v in self._mapping.items()]

    def __contains__(self, key):
        return key in self._mapping

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def __getitem__(self, key):
        return self._mapping[key]

    def __iter__(self):
        return iter(self._tags)


def run_tests():
    assert parse_year('1850-03-15') == 1850
    assert parse_year('before:1200') == 1200
    assert parse_year('C19') == 1800
    assert parse_year(None) is None

    assert is_admin_boundary_value('0')
    assert is_admin_boundary_value('11')
    assert not is_admin_boundary_value('12')
    assert not is_admin_boundary_value('region')

    assert is_misc_boundary_value('region')
    assert is_misc_boundary_value('historic_district')
    assert not is_misc_boundary_value('administrative')

    tags = FakeTags({'name': 'Test Region', 'boundary': 'administrative', 'admin_level': '4'})
    extracted = process_relation_tags(tags)
    assert extracted is not None
    assert extracted['boundary_field'] == '4'

    tags = FakeTags({'name': 'Historic Region', 'boundary': 'region', 'start_date': 'C19'})
    extracted = process_relation_tags(tags)
    assert extracted is not None
    assert extracted['boundary_field'] == 'region'
    assert build_timespans(extracted) == [{'start': {'in': 1800}}]

    tags = FakeTags({'name': 'No Boundary'})
    assert process_relation_tags(tags) is None

    print('Shared OSM/OHM boundary geometry helper tests passed.')


if __name__ == '__main__':
    run_tests()



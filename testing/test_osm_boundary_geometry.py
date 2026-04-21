#!/usr/bin/env python3
"""Lightweight regression checks for shared OSM/OHM boundary geometry helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import processing.osm_boundary_geometry as obg
from processing.osm_boundary_geometry import (
    build_h3_fields_for_geom_entry,
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

    geom_entry = {
        'repr_point': {'lon': 1.5, 'lat': 2.5},
        'hull': {'type': 'Polygon', 'coordinates': [[[0.0, 0.0], [3.0, 0.0], [3.0, 3.0], [0.0, 0.0]]]},
    }
    raw_geom = {'type': 'MultiPolygon', 'coordinates': [[[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 0.0]]]]}

    original_select = obg.select_h3_cover_geometry
    original_compute = obg.compute_h3_fields
    captured = {}
    try:
        def fake_select(entry, raw):
            captured['select_args'] = (entry, raw)
            return {'type': 'Polygon', 'coordinates': [[[9.0, 9.0], [10.0, 9.0], [10.0, 10.0], [9.0, 9.0]]]}

        def fake_compute(lon, lat, geom):
            captured['compute_args'] = (lon, lat, geom)
            return '872830828ffffff', ['872830828ffffff', '87283082cffffff']

        obg.select_h3_cover_geometry = fake_select
        obg.compute_h3_fields = fake_compute

        assert build_h3_fields_for_geom_entry(geom_entry, raw_geom) == {
            'h3_centroid': '872830828ffffff',
            'h3_cover': ['872830828ffffff', '87283082cffffff'],
        }
        assert captured['select_args'] == (geom_entry, raw_geom)
        assert captured['compute_args'] == (
            1.5,
            2.5,
            {'type': 'Polygon', 'coordinates': [[[9.0, 9.0], [10.0, 9.0], [10.0, 10.0], [9.0, 9.0]]]},
        )
        assert build_h3_fields_for_geom_entry({}, raw_geom) is None
    finally:
        obg.select_h3_cover_geometry = original_select
        obg.compute_h3_fields = original_compute

    print('Shared OSM/OHM boundary geometry helper tests passed.')


if __name__ == '__main__':
    run_tests()



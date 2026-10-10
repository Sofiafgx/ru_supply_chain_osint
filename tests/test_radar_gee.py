import math
import unittest
from unittest.mock import Mock

import sentinel1_radar_gee as gee
import sentinel1_radar_ttr as radar


class CloudRadarTests(unittest.TestCase):
    def properties(self):
        return dict(scene_id='S1A_TEST', asset_id='COPERNICUS/S1_GRD/S1A_TEST',
                    date='2025-02-06T00:00:00.000Z', orbit_number=40000,
                    relative_orbit=6, orbit_direction='DESCENDING', platform_number='A',
                    beam_mode='IW', footprint_covers=True, total_pixel_count=100,
                    VV_mean_linear=.1, VH_mean_linear=.01, VV_count=100, VH_count=98)

    def test_linear_power_to_db(self):
        rows = gee.rows_from_metrics(self.properties())
        self.assertEqual([r['mean_backscatter_db'] for r in rows], [-10, -20])
        self.assertEqual(rows[0]['platform'], 'Sentinel-1A')
        self.assertEqual(rows[1]['valid_fraction'], .98)

    def test_missing_pixels_do_not_change_baseline(self):
        props = self.properties() | {'VV_count': 30, 'VH_count': None, 'VH_mean_linear': None}
        rows = gee.rows_from_metrics(props)
        self.assertTrue(all(r['mean_backscatter_db'] is None for r in rows))
        self.assertIn('insufficient_valid_coverage', rows[0]['quality_reasons'])

    def test_nonpositive_and_nonfinite_power(self):
        for value in (None, 0, -1, float('nan'), float('inf')):
            rows = gee.rows_from_metrics(self.properties() | {'VV_mean_linear': value})
            self.assertIsNone(rows[0]['mean_backscatter_db'])
            self.assertEqual(rows[1]['quality_status'], 'usable')

    def test_partial_footprint_and_small_polygon(self):
        for change in ({'footprint_covers': False}, {'total_pixel_count': 10, 'VV_count': 10, 'VH_count': 10}):
            self.assertTrue(all(r['mean_backscatter_db'] is None
                for r in gee.rows_from_metrics(self.properties() | change)))

    def test_unknown_track_rejected(self):
        with self.assertRaises(ValueError):
            gee.rows_from_metrics(self.properties() | {'relative_orbit': None})

    def test_query_end_is_inclusive(self):
        from datetime import timedelta
        ee = Mock()
        chain = ee.ImageCollection.return_value
        chain.filterBounds.return_value = chain
        chain.filterDate.return_value = chain
        chain.filter.return_value = chain
        chain.sort.return_value = chain
        start, end = radar.timestamp('2025-01-01'), radar.timestamp('2025-08-01')
        gee.collection_for(ee, 'region', start, end)
        chain.filterDate.assert_called_once_with(radar.iso(start), radar.iso(end + timedelta(milliseconds=1)))
        ee.Filter.inList.assert_called_once_with('orbitProperties_pass', ['ASCENDING', 'DESCENDING'])

    def test_uses_existing_recovery_detector(self):
        from datetime import timedelta
        event = radar.timestamp('2025-01-31')
        rows = []
        for day, power in [(-30, .1), (-18, .1), (-6, .1), (6, .03), (18, .03), (30, .1), (42, .1)]:
            props = self.properties() | {'date': radar.iso(event + timedelta(days=day)),
                    'scene_id': f'S1A_{day}', 'VV_mean_linear': power, 'VH_mean_linear': power / 10}
            rows.extend(gee.rows_from_metrics(props))
        results = radar.assess(rows, event)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r['status'] == 'radar_recovery_candidate' for r in results))
        self.assertTrue(all(math.isclose(r['TTR_radar'], 30 / 30.4) for r in results))

    def test_default_geojson_crs(self):
        for geom in radar.load_aois().values():
            self.assertEqual(gee.reduction_crs(geom), 'EPSG:32639')


# The SDK's static API catalogue validates graph construction without credentials.
import ee
from ee.apitestcase import ApiTestCase


class CloudGraphTests(ApiTestCase):
    def test_server_graph_and_batch_serialization(self):
        from shapely.geometry import mapping
        region = ee.Geometry(mapping(radar.load_aois()['building_1']), geodesic=False)
        collection = gee.collection_for(ee, region, radar.timestamp('2025-01-01'), radar.timestamp('2025-08-01'))
        features = ee.FeatureCollection(collection.toList(20).map(
            lambda value: gee.statistic_feature(ee, value, region, 'EPSG:32639', ee.Number(100))))
        serialized = features.serialize()
        self.assertIn('Image.pow', serialized)
        self.assertIn('Image.reduceRegion', serialized)
        self.assertIn('Geometry.contains', serialized)
        self.assertIn('Reducer.unweighted', serialized)


if __name__ == '__main__':
    unittest.main()

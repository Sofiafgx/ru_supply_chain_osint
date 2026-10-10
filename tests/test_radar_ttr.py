import math
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

import sentinel1_radar_ttr as radar


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.event = radar.timestamp('2025-01-31')

    def rows(self, values):
        from datetime import timedelta
        return [dict(date=radar.iso(self.event + timedelta(days=day)), scene_id=f'scene_{i}',
                     mean_backscatter_db=value, relative_change_factor=None,
                     platform='Sentinel-1A', relative_orbit=100, orbit_number=40000 + i,
                     orbit_direction='ASCENDING', polarization='VV')
                for i, (day, value) in enumerate(values)]

    def test_drop_and_recovery(self):
        rows = self.rows([(-30, -10), (-18, -10.1), (-6, -9.9),
                          (6, -15), (18, -14), (30, -10.2), (42, -10)])
        result = radar.assess(rows, self.event)[0]
        self.assertEqual(result['status'], 'radar_recovery_candidate')
        self.assertAlmostEqual(result['TTR_radar'], 30 / 30.4)
        self.assertAlmostEqual(rows[3]['relative_change_factor'], 10 ** (-5 / 10))
        self.assertEqual(result['recovery_interval']['after'], rows[4]['date'])

    def test_single_outlier_is_not_drop(self):
        rows = self.rows([(-30, -10), (-18, -10), (-6, -10), (6, -15), (18, -10)])
        self.assertEqual(radar.assess(rows, self.event)[0]['status'], 'no_confirmed_drop')

    def test_duplicates_not_independent(self):
        rows = self.rows([(-30, -10), (-18, -10), (-6, -10), (6, -15), (6, -15)])
        self.assertEqual(radar.assess(rows, self.event)[0]['status'], 'no_confirmed_drop')

    def test_error_and_gap_break_confirmation(self):
        for last, value in [(18, None), (30, -15)]:
            rows = self.rows([(-30, -10), (-18, -10), (-6, -10), (6, -15), (last, value)])
            self.assertEqual(radar.assess(rows, self.event)[0]['status'], 'no_confirmed_drop')

    def test_orbits_not_mixed(self):
        rows = self.rows([(-30, -10), (-18, -10), (-6, -10), (6, -15), (18, -15)])
        rows[-1]['orbit_direction'] = 'DESCENDING'
        outcomes = radar.assess(rows, self.event)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(r['TTR_radar'] is None for r in outcomes))

    def test_insufficient_baseline(self):
        rows = self.rows([(-18, -10), (-6, -10), (6, -15), (18, -15), (30, -10), (42, -10)])
        self.assertEqual(radar.assess(rows, self.event)[0]['status'], 'insufficient_baseline')

    def test_instant_large_overshoot_not_recovery(self):
        rows = self.rows([(-30, -10), (-18, -10), (-6, -10), (6, -15), (18, -15), (30, -5), (42, -5)])
        self.assertIsNone(radar.assess(rows, self.event)[0]['TTR_radar'])

    def test_graph_linear_sigma0(self):
        geom = radar.load_aois()['building_1']
        graph = ET.fromstring(radar.graph_xml(Path('/tmp/scene.zip'), Path('/tmp/output'), geom))
        self.assertEqual(graph.find("node[@id='cal']/parameters/outputImageScaleInDb").text, 'false')
        self.assertEqual(graph.find("node[@id='tc']/parameters/applyRadiometricNormalization").text, 'false')
        self.assertEqual(len(graph.findall("node/operator[.='Write']")), 2)

    def test_average_in_linear_domain_and_coverage(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        from shapely.geometry import box
        with tempfile.TemporaryDirectory() as directory:
            for pol in ('VV', 'VH'):
                with rasterio.open(Path(directory) / f'Sigma0_{pol}.tif', 'w', driver='GTiff',
                        height=10, width=10, count=1, dtype='float32', crs='EPSG:4326',
                        transform=from_origin(0, 1, .1, .1), nodata=0) as ds:
                    ds.write(np.tile(np.array([1, 100], dtype='float32'), (10, 5)), 1)
            result = radar.polygon_stats(directory, box(0, 0, 1, 1))
            self.assertAlmostEqual(result['VV']['mean_backscatter_db'], 10 * math.log10(50.5))
            with self.assertRaises(ValueError):
                radar.polygon_stats(directory, box(-.1, 0, 1, 1))


if __name__ == '__main__':
    unittest.main()

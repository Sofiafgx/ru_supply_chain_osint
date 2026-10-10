import unittest
from unittest.mock import Mock
from datetime import timedelta
from shapely.geometry import box, mapping
from pystac import Item
from satellite_ttr import (parse_time, firms_events, sentinel_scenes,
                           sampling_bins, assess_object, radius_bbox)


class SatelliteTTRTests(unittest.TestCase):
    def test_firms_exact_48h_and_radius(self):
        s = Mock()
        s.get.return_value.status_code = 200
        s.get.return_value.text = (
            'latitude,longitude,acq_date,acq_time\n'
            '57.0543,53.9876,2026-09-01,1130\n'
            '57.0543,53.9876,2026-09-01,1230\n'
            '57.0543,53.9876,2026-09-03,1229\n'
            '57.0543,53.9876,2026-09-03,1230\n'
            '57.0800,54.0300,2026-09-02,0030\n')
        hits = firms_events(s, 'secret', 'MODIS_NRT', 57.0543, 53.9876,
                            parse_time('2026-09-01T12:30:00Z'))
        self.assertEqual(len(hits), 2)
        self.assertIn('/3/2026-09-01', s.get.call_args.args[0])
        self.assertLess(radius_bbox(57.0543, 53.9876)[0], 53.9876)

    def test_full_coverage_and_strict_cloud_limit(self):
        aoi = box(10, 50, 10.01, 50.01)
        dt = parse_time('2026-09-02')
        items = [Item('good', mapping(aoi.buffer(.1)), None, dt, {'eo:cloud_cover': 14}),
                 Item('partial', mapping(box(10, 50, 10.005, 50.005)), None, dt, {'eo:cloud_cover': 0}),
                 Item('cloudy', mapping(aoi), None, dt, {'eo:cloud_cover': 15})]
        client = Mock()
        client.search.return_value.items.return_value = iter(items)
        result = sentinel_scenes(client, aoi, parse_time('2026-09-01'), parse_time('2026-09-30'))
        self.assertEqual([s['scene_id'] for s in result], ['good'])
        self.assertNotIn('max_items', client.search.call_args.kwargs)

    def fixture(self):
        scenes = [{'scene_id': f's{i}', 'acquired_at': f'2026-09-{day:02d}T10:00:00Z',
                   'cloud_cover': 2} for i, day in enumerate([2, 9, 16])]
        review = {'baseline_reference': 'dated baseline file', 'damage_scene_id': 's0',
                  'damage_verified': True, 'reviewer': 'analyst', 'scenes': [
                      {'scene_id': s['scene_id'], 'local_clear': True,
                       'resolution_sufficient': True, 'same_contour_verified': True,
                       'new_roof_material': i > 0, 'roof_complete': i > 0, 'evidence': 'comparison file'}
                      for i, s in enumerate(scenes)]}
        return {'scenes': scenes}, review

    def test_recovery_interval_and_repeat_confirmation(self):
        obj, review = self.fixture()
        result = assess_object(obj, review, parse_time('2026-09-01T23:00:00Z'))
        self.assertEqual(result['ttr_days'], 8)
        self.assertEqual(result['scene_id'], 's1')
        self.assertEqual(result['confirmation_scene_id'], 's2')
        self.assertEqual(result['interval']['after'], obj['scenes'][0]['acquired_at'])
        review['scenes'][2]['roof_complete'] = False
        self.assertIsNone(assess_object(obj, review, parse_time('2026-09-01'))['ttr_days'])

    def test_partial_materials_are_not_recovery_and_pending_not_zero(self):
        obj, review = self.fixture()
        review['scenes'][1]['roof_complete'] = False
        result = assess_object(obj, review, parse_time('2026-09-01'))
        self.assertIsNone(result['ttr_days'])
        self.assertEqual(result['t_material'], obj['scenes'][1]['acquired_at'])
        review['damage_verified'] = False
        self.assertEqual(assess_object(obj, review, parse_time('2026-09-01'))['status'], 'unconfirmed')

    def test_sampling_retains_empty_bins(self):
        start = parse_time('2026-09-01')
        bins = sampling_bins([], start, start + timedelta(days=15), 7)
        self.assertEqual(len(bins), 3)
        self.assertTrue(all(b['scene_id'] is None for b in bins))

    def test_timezone_and_naive_timestamp(self):
        self.assertEqual(parse_time('2026-09-01T15:00:00+03:00').hour, 12)
        with self.assertRaises(ValueError):
            parse_time('2026-09-01T15:00:00')


if __name__ == '__main__':
    unittest.main()

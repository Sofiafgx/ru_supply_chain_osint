#!/usr/bin/env python3
"""Server-side Sentinel-1 sigma0 polygon statistics with Google Earth Engine.

Uses preprocessed COPERNICUS/S1_GRD (dB), no SNAP or full-scene downloads.
Shares AOIs and change detector with sentinel1_radar_ttr.py.
"""
import argparse
from datetime import timedelta
import math
import os
from pathlib import Path
import sys

from sentinel1_radar_ttr import assess, iso, load_aois, save, timestamp

COLLECTION = 'COPERNICUS/S1_GRD'


def reduction_crs(geom):
    lon, lat = geom.centroid.x, geom.centroid.y
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return f'EPSG:{(32600 if lat >= 0 else 32700) + zone}'


def collection_for(ee, region, start, end):
    # EE filterDate has an EXCLUSIVE end; add 1ms to include the requested endpoint.
    return (ee.ImageCollection(COLLECTION)
            .filterBounds(region)
            .filterDate(iso(start), iso(end + timedelta(milliseconds=1)))
            .filter(ee.Filter.eq('instrumentMode', 'IW'))
            .filter(ee.Filter.eq('resolution_meters', 10))
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
            .filter(ee.Filter.inList('orbitProperties_pass', ['ASCENDING', 'DESCENDING']))
            .sort('system:time_start'))


def statistic_feature(ee, value, region, crs, total):
    """Build a deferred graph; calibrated sigma0 is averaged ON THE SERVER."""
    image = ee.Image(value)
    db = image.select(['VV', 'VH'])
    # S1_GRD is already calibrated dB; do not calibrate or log it a second time.
    power = ee.Image.constant(10).pow(db.divide(10)).rename(['VV', 'VH'])
    reducer = ee.Reducer.mean().unweighted().combine(ee.Reducer.count(), sharedInputs=True)
    metrics = power.reduceRegion(reducer=reducer, geometry=region, crs=crs,
                                 scale=10, maxPixels=10_000_000, tileScale=2,
                                 bestEffort=False)
    props = {
        'scene_id': image.get('system:index'), 'asset_id': image.id(),
        'date': image.date().format("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", 'UTC'),
        'orbit_number': image.get('orbitNumber_start'),
        'relative_orbit': image.get('relativeOrbitNumber_start'),
        'orbit_direction': image.get('orbitProperties_pass'),
        'platform_number': image.get('platform_number'),
        'beam_mode': image.get('instrumentMode'),
        'total_pixel_count': total,
        'footprint_covers': image.geometry().contains(region, maxError=1),
        'VV_mean_linear': metrics.get('VV_mean'), 'VH_mean_linear': metrics.get('VH_mean'),
        'VV_count': metrics.get('VV_count'), 'VH_count': metrics.get('VH_count'),
    }
    return ee.Feature(None, props)


def fetch_batches(ee, collection, region, crs, batch_size):
    # Use an unmasked constant on the SAME output grid to count all AOI centres,
    # including pixels missing in the source image.
    total = (ee.Image.constant(1).rename('total')
             .reduceRegion(reducer=ee.Reducer.count(), geometry=region, crs=crs,
                           scale=10, maxPixels=10_000_000, bestEffort=False).get('total'))
    size = int(collection.size().getInfo())
    for offset in range(0, size, batch_size):
        values = collection.toList(batch_size, offset)
        features = ee.FeatureCollection(values.map(
            lambda value: statistic_feature(ee, value, region, crs, total)))
        # One request per batch; never bring the full raster to the client.
        payload = features.getInfo()
        yield [feature['properties'] for feature in payload['features']]


def rows_from_metrics(props, min_pixels=20, min_coverage=0.95):
    """Validate each observation; unusable dates stay in the detector as barriers."""
    required = ('scene_id', 'date', 'orbit_number', 'relative_orbit',
                'orbit_direction', 'platform_number', 'beam_mode')
    if any(props.get(key) is None for key in required):
        raise ValueError('Missing scene/track metadata')
    metadata = {key: props[key] for key in ('scene_id', 'date', 'orbit_number',
                                           'relative_orbit', 'orbit_direction', 'beam_mode')}
    metadata['date'] = iso(timestamp(metadata['date']))
    metadata['orbit_number'] = int(metadata['orbit_number'])
    metadata['relative_orbit'] = int(metadata['relative_orbit'])
    metadata['platform'] = f"Sentinel-1{props['platform_number']}"
    metadata['asset_id'] = props.get('asset_id')
    total = int(props.get('total_pixel_count') or 0)
    rows = []
    for pol in ('VV', 'VH'):
        count = int(props.get(f'{pol}_count') or 0)
        fraction = count / total if total else 0.0
        value = props.get(f'{pol}_mean_linear')
        reasons = []
        if props.get('footprint_covers') is not True:
            reasons.append('incomplete_scene_footprint')
        if count < min_pixels:
            reasons.append('too_few_pixels')
        if fraction < min_coverage:
            reasons.append('insufficient_valid_coverage')
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            reasons.append('invalid_linear_sigma0')
        rows.append(metadata | {
            'polarization': pol, 'mean_backscatter_db': None if reasons else 10 * math.log10(float(value)),
            'relative_change_factor': None, 'valid_pixel_count': count,
            'total_pixel_count': total, 'valid_fraction': fraction,
            'quality_status': 'unusable' if reasons else 'usable', 'quality_reasons': reasons,
        })
    return rows


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--t-event', required=True)
    parser.add_argument('--project', default=os.environ.get('EE_PROJECT'), help='Registered Earth Engine Google Cloud project ID')
    parser.add_argument('--authenticate', action='store_true', help='Start interactive Earth Engine OAuth login')
    parser.add_argument('--aoi', help='Optional WGS84 FeatureCollection; defaults to both supplied rectangles')
    parser.add_argument('--out', type=Path, default=Path('data/radar_ttr/gee_result.json'))
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--min-pixels', type=int, default=20)
    parser.add_argument('--min-coverage', type=float, default=.95)
    parser.add_argument('--drop-db', type=float, default=3)
    parser.add_argument('--robust-z', type=float, default=3)
    parser.add_argument('--min-baseline', type=int, default=3)
    parser.add_argument('--persistence', type=int, default=2)
    parser.add_argument('--immediate-days', type=float, default=24)
    parser.add_argument('--max-gap-days', type=float, default=18)
    parser.add_argument('--recovery-tolerance-db', type=float, default=1.5)
    parser.add_argument('--recovery-step-db', type=float, default=2)
    return parser


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    if not args.project:
        parser.error('Provide --project or EE_PROJECT; see README_RADAR_CLOUD_UK.md')
    numbers = (args.drop_db, args.robust_z, args.immediate_days, args.max_gap_days,
               args.recovery_tolerance_db, args.recovery_step_db)
    if (any(not math.isfinite(x) or x <= 0 for x in numbers) or args.min_baseline < 3
        or args.persistence < 2 or args.min_pixels < 1 or not 1 <= args.batch_size <= 100
        or not 0 < args.min_coverage <= 1 or args.recovery_tolerance_db >= args.drop_db):
        parser.error('Invalid thresholds, coverage or batch size')
    try:
        event = timestamp(args.t_event)
        aois = load_aois(args.aoi)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    import ee
    from shapely.geometry import mapping
    if args.authenticate:
        ee.Authenticate()
    try:
        ee.Initialize(project=args.project)
    except Exception as exc:
        print(f'Earth Engine initialization failed ({type(exc).__name__}). '
              'Check project access; authenticate with earthengine authenticate.', file=sys.stderr)
        return 2
    start, end = event - timedelta(days=30), event + timedelta(days=180)
    settings = dict(min_baseline=args.min_baseline, drop_db=args.drop_db, z=args.robust_z,
                    persistence=args.persistence, immediate_days=args.immediate_days,
                    max_gap_days=args.max_gap_days, recovery_tolerance_db=args.recovery_tolerance_db,
                    recovery_step_db=args.recovery_step_db)
    doc = {'status': 'running', 'source': 'Google Earth Engine', 'collection': COLLECTION,
           't_event': iso(event), 'query_interval': [iso(start), iso(end)],
           'quantity': 'sigma0', 'processing': 'Provider-preprocessed GRD; polygon reductions on Earth Engine',
           'mean_definition': '10*log10(mean(10**(sigma0_db/10)))',
           'factor_definition': '10**((mean_backscatter_db - median_pre_event_db)/10)',
           'parameters': settings | {'min_pixels': args.min_pixels, 'min_coverage': args.min_coverage,
                                    'beam_mode': 'IW', 'resolution_meters': 10},
           'interpretation': 'Radar change candidate; does not prove roof material or operational recovery.',
           'objects': {}}
    failed = False
    for oid, geom in aois.items():
        crs = reduction_crs(geom)
        obj = {'geometry': mapping(geom), 'reduction_crs': crs, 'reduction_scale_m': 10,
               'time_series': [], 'assessment': [], 'errors': [], 'status': 'running'}
        doc['objects'][oid] = obj
        region = ee.Geometry(mapping(geom), proj='EPSG:4326', geodesic=False)
        collection = collection_for(ee, region, start, end)
        print(f'{oid}: requesting server-side sigma0 statistics', file=sys.stderr)
        try:
            for batch in fetch_batches(ee, collection, region, crs, args.batch_size):
                for props in batch:
                    obj['time_series'].extend(rows_from_metrics(props, args.min_pixels, args.min_coverage))
                save(args.out, doc)
        except Exception as exc:
            # A failed batch may hide an unknown observation: do not assess a partial collection.
            obj['errors'].append({'stage': 'server_statistics', 'error_type': type(exc).__name__})
            obj['status'] = 'incomplete_collection'
            failed = True
            save(args.out, doc)
            continue
        obj['time_series'].sort(key=lambda r: (r['date'], r['scene_id'], r['polarization']))
        obj['assessment'] = assess(obj['time_series'], event, **settings)
        usable = sum(r['quality_status'] == 'usable' for r in obj['time_series'])
        obj['status'] = ('no_scenes' if not obj['time_series'] else
                         'no_usable_observations' if not usable else 'complete')
        save(args.out, doc)
    doc['status'] = 'partial_error' if failed else 'complete'
    save(args.out, doc)
    print(f'Saved {args.out.resolve()}')
    return 2 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

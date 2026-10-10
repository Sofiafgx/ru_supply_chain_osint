#!/usr/bin/env python3
"""ASF Sentinel-1 GRD -> SNAP linear sigma0 -> polygon time series (UTC).

No radiometric terrain flattening: the measured quantity is sigma0, not gamma0.
Run --catalog-only without Earthdata credentials or SNAP; see README_RADAR_TTR_UK.md.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        if len(value) != 10:
            raise ValueError('Timestamp requires a timezone; date-only means 00:00 UTC')
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


def save(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def load_aois(path=None):
    from shapely.geometry import shape
    from shapely import wkt
    if path:
        doc = json.loads(Path(path).read_text(encoding='utf-8'))
        if doc.get('type') != 'FeatureCollection':
            raise ValueError('AOI must be a GeoJSON FeatureCollection')
        entries = [(f['properties']['object_id'], shape(f['geometry'])) for f in doc['features']]
    else:
        entries = [('building_1', wkt.loads('POLYGON((53.9860 57.0535,53.9890 57.0535,53.9890 57.0555,53.9860 57.0555,53.9860 57.0535))')),
                   ('building_2', wkt.loads('POLYGON((52.0500 55.8230,52.0530 55.8230,52.0530 55.8255,52.0500 55.8255,52.0500 55.8230))'))]
    aois = {}
    for oid, geom in entries:
        if not isinstance(oid, str) or not oid or oid in aois:
            raise ValueError('object_id must be a unique nonempty string')
        if geom.geom_type not in ('Polygon', 'MultiPolygon') or geom.is_empty or not geom.is_valid:
            raise ValueError(f'{oid}: invalid polygon')
        west, south, east, north = geom.bounds
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError(f'{oid}: use WGS84 longitude, latitude')
        aois[oid] = geom
    if not aois:
        raise ValueError('Empty AOI collection')
    return aois


def search(geom, start, end):
    import asf_search as asf
    from shapely.geometry import shape
    scenes = {}
    for direction in ('ASCENDING', 'DESCENDING'):
        # search_generator follows all pages; no maxResults truncation.
        pages = asf.search_generator(dataset=asf.DATASET.SENTINEL1,
            processingLevel=asf.PRODUCT_TYPE.GRD_HD, beamMode='IW',
            polarization=asf.POLARIZATION.VV_VH, flightDirection=direction,
            intersectsWith=geom.wkt, start=iso(start), end=iso(end))
        for page in pages:
            for product in page:
                p = product.properties
                dt = timestamp(p['startTime'])
                if not start <= dt <= end or not shape(product.geometry).covers(geom):
                    continue
                # Metadata fields must be present; never merge unknown tracks.
                sid = p['sceneName']
                record = {'scene_id': sid, 'date': iso(dt),
                    'orbit_number': int(p['orbit']), 'relative_orbit': int(p['pathNumber']),
                    'orbit_direction': direction, 'platform': p['platform'], 'beam_mode': 'IW',
                    'url': p['url'], 'file_name': p['fileName']}
                scenes[sid] = (record, product)
    return sorted(scenes.values(), key=lambda entry: (entry[0]['date'], entry[0]['scene_id']))


def graph_xml(source, target, geom):
    """Calibrate before subsetting so noise LUTs remain available for the full swath."""
    lon, lat = geom.centroid.x, geom.centroid.y
    epsg = (32600 if lat >= 0 else 32700) + min(60, int((lon + 180) // 6) + 1)
    root = ET.Element('graph', id='sigma0')
    ET.SubElement(root, 'version').text = '1.0'
    previous = None
    steps = [
        ('read', 'Read', {'file': str(source)}),
        ('orbit', 'Apply-Orbit-File', {'orbitType': 'Sentinel Precise (Auto Download)', 'continueOnFail': 'false'}),
        ('border', 'Remove-GRD-Border-Noise', {'selectedPolarisations': 'VV,VH'}),
        ('noise', 'ThermalNoiseRemoval', {'selectedPolarisations': 'VV,VH', 'removeThermalNoise': 'true'}),
        ('cal', 'Calibration', {'selectedPolarisations': 'VV,VH', 'outputSigmaBand': 'true',
                               'outputBetaBand': 'false', 'outputGammaBand': 'false', 'outputImageScaleInDb': 'false'}),
        ('subset', 'Subset', {'geoRegion': geom.buffer(0.02).envelope.wkt, 'copyMetadata': 'true'}),
        ('tc', 'Terrain-Correction', {'sourceBands': 'Sigma0_VV,Sigma0_VH', 'demName': 'SRTM 1Sec HGT',
             'demResamplingMethod': 'BILINEAR_INTERPOLATION', 'imgResamplingMethod': 'BILINEAR_INTERPOLATION',
             'pixelSpacingInMeter': '10.0', 'mapProjection': f'EPSG:{epsg}',
             'nodataValueAtSea': 'false', 'saveSelectedSourceBand': 'true',
             'applyRadiometricNormalization': 'false'}),
        ]
    for name, op, params in steps:
        node = ET.SubElement(root, 'node', id=name)
        ET.SubElement(node, 'operator').text = op
        if previous:
            ET.SubElement(ET.SubElement(node, 'sources'), 'sourceProduct', refid=previous)
        parameters = ET.SubElement(node, 'parameters')
        for k, v in params.items():
            ET.SubElement(parameters, k).text = v
        previous = name
    # Separate named single-band files avoid relying on GeoTIFF band descriptions.
    for pol in ('VV', 'VH'):
        select = ET.SubElement(root, 'node', id=f'select_{pol}')
        ET.SubElement(select, 'operator').text = 'BandSelect'
        ET.SubElement(ET.SubElement(select, 'sources'), 'sourceProduct', refid='tc')
        params = ET.SubElement(select, 'parameters')
        ET.SubElement(params, 'sourceBands').text = f'Sigma0_{pol}'
        writer = ET.SubElement(root, 'node', id=f'write_{pol}')
        ET.SubElement(writer, 'operator').text = 'Write'
        ET.SubElement(ET.SubElement(writer, 'sources'), 'sourceProduct', refid=f'select_{pol}')
        params = ET.SubElement(writer, 'parameters')
        ET.SubElement(params, 'file').text = str(target / f'Sigma0_{pol}.tif')
        ET.SubElement(params, 'formatName').text = 'GeoTIFF'
    return ET.tostring(root, encoding='unicode')


def process_scene(record, product, geom, cache, gpt, session, memory):
    # Geometry and pipeline version enter the key: a different AOI cannot reuse an old crop.
    key = hashlib.sha256((record['scene_id'] + geom.wkt + 'sigma0-v2').encode()).hexdigest()[:24]
    work = cache / key
    work.mkdir(parents=True, exist_ok=True)
    outputs = [work / f'Sigma0_{pol}.tif' for pol in ('VV', 'VH')]
    if (work / 'complete.json').exists() and all(p.exists() for p in outputs):
        return work
    name = Path(record['file_name']).name
    if name != record['file_name']:
        raise ValueError('Unsafe archive filename in catalogue')
    archive = cache / name
    if not archive.exists():
        if session is None:
            raise RuntimeError('Set EARTHDATA_TOKEN or EARTHDATA_USERNAME/EARTHDATA_PASSWORD')
        product.download(path=str(cache), session=session)
    if not zipfile.is_zipfile(archive):
        archive.unlink(missing_ok=True)
        raise RuntimeError('Downloaded product is not a ZIP archive; retry the download')
    graph = work / 'graph.xml'
    graph.write_text(graph_xml(archive, work, geom), encoding='utf-8')
    with (work / 'snap.log').open('w', encoding='utf-8') as log:
        try:
            subprocess.run([gpt, str(graph), '-c', memory], stdout=log, stderr=subprocess.STDOUT, check=True)
            if not all(p.exists() for p in outputs):
                raise RuntimeError('SNAP produced incomplete GeoTIFFs')
        except Exception:
            for output in outputs:
                output.unlink(missing_ok=True)
            raise RuntimeError(f'SNAP processing failed; see {work / "snap.log"}') from None
    save(work / 'complete.json', {'scene_id': record['scene_id'], 'quantity': 'linear_sigma0'})
    return work


def polygon_stats(path, geom, min_coverage=0.95, min_pixels=20):
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.mask import mask
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping, shape, box
    stats = {}
    for pol in ('VV', 'VH'):
        with rasterio.open(Path(path) / f'Sigma0_{pol}.tif') as ds:
            if ds.crs is None or ds.count != 1:
                raise ValueError('Expected one georeferenced linear sigma0 band')
            projected = transform_geom('EPSG:4326', ds.crs, mapping(geom))
            if not box(*ds.bounds).covers(shape(projected)):
                raise ValueError('Raster does not cover the entire AOI')
            data, transform = mask(ds, [projected], crop=True, filled=False, all_touched=False)
            inside = geometry_mask([projected], data.shape[1:], transform, invert=True, all_touched=False)
            total = int(inside.sum())
            band = data[0]
            valid = inside & ~np.ma.getmaskarray(band) & np.isfinite(band.data) & (band.data > 0)
            count = int(valid.sum())
            coverage = count / total if total else 0
            if count < min_pixels or coverage < min_coverage:
                raise ValueError(f'{pol}: insufficient coverage/pixels: {coverage:.3f}/{count}')
            # Average power FIRST, then dB; do not average per-pixel logarithms.
            power = float(np.mean(band.data[valid], dtype=np.float64))
            stats[pol] = {'mean_backscatter_db': 10 * math.log10(power),
                          'valid_pixel_count': count, 'valid_fraction': coverage}
    return stats


def median(values):
    values = sorted(values)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def assess(rows, event, min_baseline=3, drop_db=3.0, z=3.0, recovery_tolerance_db=1.5,
           recovery_step_db=2.0, immediate_days=24, max_gap_days=18, persistence=2):
    """Heuristic robust change screening, not a p-value or proof of roof material."""
    groups = defaultdict(list)
    for row in rows:
        key = tuple(row[k] for k in ('platform', 'relative_orbit', 'orbit_direction', 'polarization'))
        groups[key].append(row)
    results = []
    for key, group in sorted(groups.items()):
        group.sort(key=lambda r: (r['date'], r['scene_id']))
        # Overlapping slices/reprocessed products on the same date aren't confirmations.
        dates = {}
        for r in group:
            day = r['date'][:10]
            score = (r['mean_backscatter_db'] is not None, r.get('valid_fraction', 0), r['scene_id'])
            old = dates.get(day)
            if old is None or score > old[0]:
                dates[day] = (score, r)
        series = [v[1] for _, v in sorted(dates.items())]
        base = [r['mean_backscatter_db'] for r in series
                if event - timedelta(days=30) <= timestamp(r['date']) < event
                and r['mean_backscatter_db'] is not None]
        result = dict(zip(('platform', 'relative_orbit', 'orbit_direction', 'polarization'), key))
        result.update(status='insufficient_baseline', baseline_count=len(base),
                      t_drop=None, t_recovery=None, TTR_radar=None, ttr_radar_candidate=None)
        results.append(result)
        if len(base) < min_baseline:
            continue
        level = median(base)
        scale = max(0.5, 1.4826 * median([abs(x - level) for x in base]))
        threshold = max(drop_db, z * scale)
        result.update(baseline_db=level, robust_scale_db=scale, drop_threshold_db=threshold,
                      status='no_confirmed_drop')
        for r in group:
            v = r['mean_backscatter_db']
            r['relative_change_factor'] = 10 ** ((v - level) / 10) if v is not None else None
        post = [r for r in series if timestamp(r['date']) >= event]

        def sustained(index, predicate):
            window = post[index:index + persistence]
            return (len(window) == persistence and
                all(r['mean_backscatter_db'] is not None and predicate(r['mean_backscatter_db']) for r in window) and
                all(0 < (timestamp(b['date']) - timestamp(a['date'])).total_seconds() / 86400 <= max_gap_days
                    for a, b in zip(window, window[1:])))

        drop_index = next((i for i, r in enumerate(post)
            if timestamp(r['date']) <= event + timedelta(days=immediate_days)
            and sustained(i, lambda value: value <= level - threshold)), None)
        if drop_index is None:
            continue
        result.update(status='drop_without_confirmed_recovery', t_drop=post[drop_index]['date'],
                      drop_scene_id=post[drop_index]['scene_id'])
        for i in range(drop_index + persistence, len(post)):
            previous, current = post[i - 1], post[i]
            p, c = previous['mean_backscatter_db'], current['mean_backscatter_db']
            gap = (timestamp(current['date']) - timestamp(previous['date'])).total_seconds() / 86400
            if (p is None or c is None or not 0 < gap <= max_gap_days
                or p >= level - recovery_tolerance_db or c - p < recovery_step_db):
                continue
            if not sustained(i, lambda value: abs(value - level) <= recovery_tolerance_db):
                continue
            recovery = timestamp(current['date'])
            result.update(status='radar_recovery_candidate', t_recovery=iso(recovery),
                recovery_scene_id=current['scene_id'], confirmation_scene_id=post[i + persistence - 1]['scene_id'],
                recovery_interval={'after': previous['date'], 'on_or_before': iso(recovery)},
                ttr_radar_candidate={'t_prev': previous['date'], 't_observed': iso(recovery),
                                     'confirms_production_recovery': False},
                TTR_radar=(recovery - event).total_seconds() / 86400 / 30.4)
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--t-event', required=True, help='ISO8601 UTC/date or timestamp with timezone')
    parser.add_argument('--aoi', help='Optional WGS84 GeoJSON FeatureCollection with object_id')
    parser.add_argument('--out', type=Path, default=Path('data/radar_ttr/result.json'))
    parser.add_argument('--cache', type=Path, default=Path('data/radar_ttr/cache'))
    parser.add_argument('--catalog-only', action='store_true')
    parser.add_argument('--gpt', default=os.environ.get('SNAP_GPT', 'gpt'))
    parser.add_argument('--snap-memory', default='4G')
    parser.add_argument('--drop-db', type=float, default=3.0)
    parser.add_argument('--robust-z', type=float, default=3.0)
    parser.add_argument('--min-baseline', type=int, default=3)
    parser.add_argument('--persistence', type=int, default=2)
    parser.add_argument('--immediate-days', type=float, default=24)
    parser.add_argument('--max-gap-days', type=float, default=18)
    parser.add_argument('--recovery-tolerance-db', type=float, default=1.5)
    parser.add_argument('--recovery-step-db', type=float, default=2)
    args = parser.parse_args()
    numeric = [args.drop_db, args.robust_z, args.immediate_days, args.max_gap_days,
               args.recovery_tolerance_db, args.recovery_step_db]
    if any(not math.isfinite(x) or x <= 0 for x in numeric) or args.min_baseline < 3 or args.persistence < 2:
        parser.error('Thresholds must be positive finite numbers; baseline >=3, persistence >=2')
    if args.recovery_tolerance_db >= args.drop_db:
        parser.error('Recovery tolerance must be smaller than the drop threshold')
    import asf_search as asf
    from shapely.geometry import mapping
    event = timestamp(args.t_event)
    start, end = event - timedelta(days=30), event + timedelta(days=180)
    aois = load_aois(args.aoi)
    gpt = shutil.which(args.gpt)
    # macOS /usr/sbin/gpt is a disk partition utility, not ESA SNAP.
    if gpt and Path(gpt).resolve() == Path('/usr/sbin/gpt'):
        gpt = None
    if not args.catalog_only and gpt is None:
        parser.error('Install ESA SNAP with Sentinel-1 toolbox and provide --gpt /path/to/gpt')
    session = None
    if not args.catalog_only:
        if os.environ.get('EARTHDATA_TOKEN'):
            session = asf.ASFSession().auth_with_token(os.environ['EARTHDATA_TOKEN'])
        elif os.environ.get('EARTHDATA_USERNAME') and os.environ.get('EARTHDATA_PASSWORD'):
            session = asf.ASFSession().auth_with_creds(os.environ['EARTHDATA_USERNAME'], os.environ['EARTHDATA_PASSWORD'])
    args.cache = args.cache.resolve()
    args.cache.mkdir(parents=True, exist_ok=True)
    doc = {'t_event': iso(event), 'query_interval': [iso(start), iso(end)], 'source': 'NASA ASF',
           'quantity': 'sigma0', 'mean_definition': '10*log10(mean(linear_sigma0))',
           'factor_definition': '10**((mean_backscatter_db - median_pre_event_db)/10)',
           'parameters': {k: v for k, v in vars(args).items() if isinstance(v, (int, float))},
           'interpretation': 'Radar change candidate; does not prove destruction, roof material or operational recovery.',
           'catalog_only': args.catalog_only, 'objects': {}}
    failed = False
    for oid, geom in aois.items():
        obj = {'geometry': mapping(geom), 'scenes': [], 'time_series': [], 'assessment': [], 'errors': []}
        doc['objects'][oid] = obj
        try:
            scenes = search(geom, start, end)
        except Exception as exc:
            obj['errors'].append({'stage': 'search', 'error_type': type(exc).__name__})
            failed = True
            obj['status'] = 'search_error'
            save(args.out, doc)
            continue
        obj['scenes'] = [record for record, _ in scenes]
        for record, product in scenes:
            if args.catalog_only:
                continue
            print(f"{oid}: {record['date']} {record['scene_id']}", file=sys.stderr)
            rows = [{k: v for k, v in record.items() if k not in ('url', 'file_name')}
                    | {'polarization': pol, 'mean_backscatter_db': None, 'relative_change_factor': None}
                    for pol in ('VV', 'VH')]
            try:
                raster = process_scene(record, product, geom, args.cache, gpt, session, args.snap_memory)
                stats = polygon_stats(raster, geom)
                for row in rows:
                    row.update(stats[row['polarization']])
            except Exception as exc:
                # No exception message: third-party transport errors can contain credentials.
                obj['errors'].append({'scene_id': record['scene_id'], 'stage': 'processing',
                                      'error_type': type(exc).__name__})
                failed = True
            obj['time_series'].extend(rows)
            save(args.out, doc)  # partial progress survives interruption
        if not args.catalog_only:
            obj['assessment'] = assess(obj['time_series'], event, min_baseline=args.min_baseline,
                drop_db=args.drop_db, z=args.robust_z, persistence=args.persistence,
                immediate_days=args.immediate_days, max_gap_days=args.max_gap_days,
                recovery_tolerance_db=args.recovery_tolerance_db, recovery_step_db=args.recovery_step_db)
        obj['status'] = ('catalog_only' if args.catalog_only else 'partial_error' if obj['errors'] else
                         'complete' if scenes else 'no_scenes')
        save(args.out, doc)
    doc['status'] = 'partial_error' if failed else 'complete'
    save(args.out, doc)
    print(f"Saved {args.out.resolve()}")
    return 2 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

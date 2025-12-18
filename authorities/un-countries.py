# processing/un-countries.py

"""
Index UN member countries with official geometries.

This script indexes current UN member states (193 countries) plus observer states
with high-quality boundary geometries from Natural Earth data.

Data sources:
- Natural Earth 1:10m Cultural Vectors (Admin 0 - Countries)
- UN member state list for validation
- ISO 3166 country codes

Each country gets:
- Official name and variants
- ISO codes (alpha-2, alpha-3, numeric)
- High-quality boundary geometry (simplified for performance)
- Representative point (capital or geometric centroid)
- UN membership status
- Region/subregion classification

Updated to use namespace 'un' and new file paths from settings.py
Updated to use temporal scoping design. UN Countries is current data (2025).
Note: settings.py specifies namespace 'un' for ISO3166 dataset
"""

import sys
import zipfile
import urllib.request
from pathlib import Path

from processing.helpers import (
    compute_geodetic_centroid,
    compute_representative_point,
    compute_bbox,
    compute_area_km2,
    simplify_geometry
)

from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

# Natural Earth download URL
NATURAL_EARTH_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"

# UN member states (193 members + 2 observer states)
UN_MEMBERS = {
    'Afghanistan', 'Albania', 'Algeria', 'Andorra', 'Angola', 'Antigua and Barbuda',
    'Argentina', 'Armenia', 'Australia', 'Austria', 'Azerbaijan', 'Bahamas', 'Bahrain',
    'Bangladesh', 'Barbados', 'Belarus', 'Belgium', 'Belize', 'Benin', 'Bhutan',
    'Bolivia', 'Bosnia and Herzegovina', 'Botswana', 'Brazil', 'Brunei', 'Bulgaria',
    'Burkina Faso', 'Burundi', 'Cabo Verde', 'Cambodia', 'Cameroon', 'Canada',
    'Central African Republic', 'Chad', 'Chile', 'China', 'Colombia', 'Comoros',
    'Congo', 'Costa Rica', 'Croatia', "Côte d'Ivoire", 'Cuba', 'Cyprus',
    'Czech Republic', 'Czechia', 'Denmark', 'Djibouti', 'Dominica', 'Dominican Republic',
    'Ecuador', 'Egypt', 'El Salvador', 'Equatorial Guinea', 'Eritrea', 'Estonia',
    'Eswatini', 'Ethiopia', 'Fiji', 'Finland', 'France', 'Gabon', 'Gambia', 'Georgia',
    'Germany', 'Ghana', 'Greece', 'Grenada', 'Guatemala', 'Guinea', 'Guinea-Bissau',
    'Guyana', 'Haiti', 'Honduras', 'Hungary', 'Iceland', 'India', 'Indonesia', 'Iran',
    'Iraq', 'Ireland', 'Israel', 'Italy', 'Jamaica', 'Japan', 'Jordan', 'Kazakhstan',
    'Kenya', 'Kiribati', 'Kuwait', 'Kyrgyzstan', 'Laos', 'Latvia', 'Lebanon', 'Lesotho',
    'Liberia', 'Libya', 'Liechtenstein', 'Lithuania', 'Luxembourg', 'Madagascar',
    'Malawi', 'Malaysia', 'Maldives', 'Mali', 'Malta', 'Marshall Islands', 'Mauritania',
    'Mauritius', 'Mexico', 'Micronesia', 'Moldova', 'Monaco', 'Mongolia', 'Montenegro',
    'Morocco', 'Mozambique', 'Myanmar', 'Namibia', 'Nauru', 'Nepal', 'Netherlands',
    'New Zealand', 'Nicaragua', 'Niger', 'Nigeria', 'North Korea', 'North Macedonia',
    'Norway', 'Oman', 'Pakistan', 'Palau', 'Panama', 'Papua New Guinea', 'Paraguay',
    'Peru', 'Philippines', 'Poland', 'Portugal', 'Qatar', 'Romania', 'Russia',
    'Rwanda', 'Saint Kitts and Nevis', 'Saint Lucia', 'Saint Vincent and the Grenadines',
    'Samoa', 'San Marino', 'Sao Tome and Principe', 'Saudi Arabia', 'Senegal', 'Serbia',
    'Seychelles', 'Sierra Leone', 'Singapore', 'Slovakia', 'Slovenia', 'Solomon Islands',
    'Somalia', 'South Africa', 'South Korea', 'South Sudan', 'Spain', 'Sri Lanka',
    'Sudan', 'Suriname', 'Sweden', 'Switzerland', 'Syria', 'Tajikistan', 'Tanzania',
    'Thailand', 'Timor-Leste', 'Togo', 'Tonga', 'Trinidad and Tobago', 'Tunisia',
    'Turkey', 'Turkmenistan', 'Tuvalu', 'Uganda', 'Ukraine', 'United Arab Emirates',
    'United Kingdom', 'United States', 'United States of America', 'Uruguay', 'Uzbekistan',
    'Vanuatu', 'Venezuela', 'Vietnam', 'Yemen', 'Zambia', 'Zimbabwe',
    # Observer states
    'Palestine', 'Holy See', 'Vatican City'
}


def download_natural_earth(data_dir):
    """
    Download Natural Earth countries data if not already present.

    Returns: Path to the downloaded ZIP file
    """
    ne_dir = Path(data_dir) / "ISO3166"
    ne_dir.mkdir(parents=True, exist_ok=True)

    zip_path = ne_dir / "ne_10m_admin_0_countries.zip"

    if zip_path.exists():
        print(f"✓ Natural Earth data already downloaded: {zip_path}")
        return zip_path

    print(f"Downloading Natural Earth 1:10m countries data...")
    print(f"URL: {NATURAL_EARTH_URL}")
    print(f"Destination: {zip_path}")

    try:
        urllib.request.urlretrieve(NATURAL_EARTH_URL, zip_path)
        print(f"✓ Download complete ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
        return zip_path
    except Exception as e:
        print(f"✗ Download failed: {e}")
        print("\nAlternative: Manually download from:")
        print("  https://www.naturalearthdata.com/downloads/10m-cultural-vectors/")
        print("  Download 'Admin 0 – Countries' and place at:")
        print(f"  {zip_path}")
        sys.exit(1)


def read_shapefile_from_zip(zip_path):
    """
    Read GeoJSON features from Natural Earth shapefile in ZIP.

    Natural Earth provides shapefiles. We'll need to convert to GeoJSON.
    For simplicity, we'll look for a GeoJSON file or convert on the fly.

    Returns: List of GeoJSON feature dicts
    """
    print(f"Reading shapefile from {zip_path}...")

    # Check if we have pyshp/shapefile library
    try:
        import shapefile
    except ImportError:
        print("\nERROR: pyshp library required to read shapefiles")
        print("Install with: pip install pyshp --break-system-packages")
        sys.exit(1)

    # Extract and read shapefile
    with zipfile.ZipFile(zip_path, 'r') as zf:
        # Find the .shp file
        shp_files = [f for f in zf.namelist() if f.endswith('.shp')]
        if not shp_files:
            print("ERROR: No .shp file found in ZIP")
            sys.exit(1)

        shp_name = shp_files[0]
        base_name = shp_name[:-4]  # Remove .shp extension

        print(f"Found shapefile: {shp_name}")

        # Extract all related files (.shp, .shx, .dbf, .prj)
        temp_dir = Path(DATA_DIR) / "ISO3166" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
            file_name = base_name + ext
            if file_name in zf.namelist():
                zf.extract(file_name, temp_dir)

        # Read shapefile
        shp_path = temp_dir / shp_name
        sf = shapefile.Reader(str(shp_path))

        features = []

        for i, shape_rec in enumerate(sf.shapeRecords()):
            # Convert to GeoJSON-like structure
            geom = shape_rec.shape.__geo_interface__
            props = shape_rec.record.as_dict()

            feature = {
                'type': 'Feature',
                'geometry': geom,
                'properties': props
            }

            features.append(feature)

        print(f"✓ Read {len(features)} country features")

        return features


def is_un_member(country_name):
    """
    Check if a country is a UN member or observer state.

    Uses flexible matching to handle name variations.
    """
    # Normalize name for comparison
    name_normalized = country_name.lower().strip()

    # Check direct match
    for un_name in UN_MEMBERS:
        if un_name.lower() == name_normalized:
            return True

        # Check if one is substring of other (e.g., "United States" vs "United States of America")
        if un_name.lower() in name_normalized or name_normalized in un_name.lower():
            if len(name_normalized) > 5:  # Avoid short false matches
                return True

    # Special cases
    if 'democratic republic of the congo' in name_normalized or 'dr congo' in name_normalized:
        return True
    if 'republic of the congo' in name_normalized or name_normalized == 'congo':
        return True
    if 'korea' in name_normalized and 'south' in name_normalized:
        return True
    if 'korea' in name_normalized and 'north' in name_normalized:
        return True

    return False


def create_country_place_doc(feature, simplification_tolerance_km=1.0):
    """
    Create a place document for a country.

    Args:
        feature: GeoJSON feature from Natural Earth
        simplification_tolerance_km: Tolerance for geometry simplification

    Returns: Place document dict
    """
    props = feature['properties']
    geometry = feature['geometry']

    # Extract key properties
    # Natural Earth field names (these may vary by version)
    name = props.get('NAME', props.get('ADMIN', 'Unknown'))
    name_long = props.get('NAME_LONG', name)
    formal_name = props.get('FORMAL_EN', name_long)

    iso_a2 = props.get('ISO_A2', props.get('ISO_A2_EH', ''))
    iso_a3 = props.get('ISO_A3', props.get('ISO_A3_EH', ''))
    iso_n3 = props.get('ISO_N3', '')

    # Create place ID using 'un' namespace as specified in settings.py
    place_id = f"un:{iso_a3.lower()}" if iso_a3 and iso_a3 != '-99' else f"un:{name.lower().replace(' ', '_')}"

    # Check UN membership
    un_member = is_un_member(name)

    # Build toponyms array with temporal scoping
    toponyms = []
    seen_names = set()

    # Add main name
    if name and name not in seen_names:
        lst = f"{name}@en"
        # Current data - use 2025
        toponyms.append({
            'toponym_id': lst,
            'timespan': {
                'start': {'in': 2025},
                'end': {'in': 2025}
            }
        })
        seen_names.add(name)

    # Add long name
    if name_long and name_long not in seen_names:
        lst = f"{name_long}@en"
        toponyms.append(lst)
        temporally_scoped_toponyms.append({
            'toponym_id': lst,
            'timespan': {
                'start': {'in': 2025},
                'end': {'in': 2025}
            }
        })
        seen_names.add(name_long)

    # Add formal name
    if formal_name and formal_name not in seen_names:
        lst = f"{formal_name}@en"
        toponyms.append(lst)
        temporally_scoped_toponyms.append({
            'toponym_id': lst,
            'timespan': {
                'start': {'in': 2025},
                'end': {'in': 2025}
            }
        })
        seen_names.add(formal_name)

    # Add other name variants if available
    for field in ['NAME_ALT', 'NAME_SORT', 'ABBREV']:
        alt_name = props.get(field)
        if alt_name and alt_name not in seen_names:
            lst = f"{alt_name}@en"
            toponyms.append({
                'toponym_id': lst,
                'timespan': {
                    'start': {'in': 2025},
                    'end': {'in': 2025}
                }
            })
            seen_names.add(alt_name)

    # Simplify geometry for performance
    simplified_geom = simplify_geometry(geometry, tolerance_km=simplification_tolerance_km)
    if not simplified_geom:
        simplified_geom = geometry

    # Compute representative point
    rep_point = compute_representative_point(simplified_geom)
    if not rep_point:
        # Fall back to centroid
        rep_point = compute_geodetic_centroid(simplified_geom)

    # Create location object
    location = {
        'geometry': simplified_geom,
        'rep_point': rep_point
    }

    # Build document
    doc = {
        'place_id': place_id,
        'label': name,
        'toponyms': toponyms,
        'locations': [location],
        'source': 'un-countries',
        'types': [{
            'identifier': 'country',
            'label': 'un',
            'sourceLabel': 'sovereign-country'
        }]
    }

    # Add ISO codes
    if iso_a2 and iso_a2 != '-99':
        doc['ccodes'] = [iso_a2]

    # Add relations for alternate codes
    relations = []
    if iso_a3 and iso_a3 != '-99':
        relations.append({
            'relationType': 'hasIdentifier',
            'relationTo': f"iso3166:{iso_a3}",
            'label': f"ISO 3166-1 alpha-3: {iso_a3}",
            'source': 'natural-earth',
            'method': 'curated',
            'certainty': 1.0
        })
    if iso_n3 and iso_n3 != '-99':
        relations.append({
            'relationType': 'hasIdentifier',
            'relationTo': f"iso3166:{iso_n3}",
            'label': f"ISO 3166-1 numeric: {iso_n3}",
            'source': 'natural-earth',
            'method': 'curated',
            'certainty': 1.0
        })

    if relations:
        doc['relations'] = relations

    # Add admin level
    doc['admin_level'] = 0  # Country level

    # Compute area
    area = compute_area_km2(simplified_geom)
    if area:
        doc['area_km2'] = round(area, 2)

    # Add additional properties from Natural Earth
    if props.get('CONTINENT'):
        doc['continent'] = props['CONTINENT']
    if props.get('SUBREGION'):
        doc['subregion'] = props['SUBREGION']
    if props.get('POP_EST'):
        try:
            doc['population_est'] = int(props['POP_EST'])
        except (ValueError, TypeError):
            pass

    return doc


def index_un_countries(
        places_index='places',
        simplification_tolerance_km=1.0,
        download=True
):
    """
    Index UN member countries with geometries.

    Args:
        places_index: Target places index
        simplification_tolerance_km: Geometry simplification tolerance
        download: Whether to download Natural Earth data if not present

    Note: With new design, we only index places.
    Toponyms will be indexed separately by cross-authority deduplication.
    """
    print("=" * 80)
    print("UN COUNTRIES INDEXING")
    print("=" * 80)
    print()

    # Download or locate Natural Earth data
    if download:
        zip_path = download_natural_earth(DATA_DIR)
    else:
        zip_path = Path(DATA_DIR) / "ISO3166" / "ne_10m_admin_0_countries.zip"
        if not zip_path.exists():
            print(f"ERROR: Natural Earth data not found at {zip_path}")
            print("Run with download=True or manually download the data")
            sys.exit(1)

    # Read features
    features = read_shapefile_from_zip(zip_path)

    print(f"\nProcessing {len(features)} countries...")
    print(f"Simplification tolerance: {simplification_tolerance_km} km")
    print()

    # Track statistics
    stats = {
        'processed': 0,
        'places_indexed': 0,
        'un_members': 0,
        'non_un': 0,
        'errors': 0
    }

    place_batch = []

    for i, feature in enumerate(features):
        try:
            # Create place document
            place_doc = create_country_place_doc(feature, simplification_tolerance_km)
            place_id = place_doc['place_id']

            # Track UN membership
            name = feature['properties'].get('NAME', feature['properties'].get('ADMIN', ''))
            if is_un_member(name):
                stats['un_members'] += 1
            else:
                stats['non_un'] += 1

            # Add to batch
            place_batch.append({
                '_index': places_index,
                '_id': place_id,
                '_source': place_doc
            })

            stats['processed'] += 1

            if (i + 1) % 50 == 0:
                print(f"Processed {i + 1} countries...")

        except Exception as e:
            print(f"Error processing country {i}: {e}")
            stats['errors'] += 1
            continue

    # Bulk index
    print("\nIndexing to Elasticsearch...")

    if place_batch:
        try:
            success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
            stats['places_indexed'] = success
        except Exception as e:
            print(f"Error indexing places: {e}")

    # Final report
    print("\n" + "=" * 80)
    print("INDEXING COMPLETE")
    print("=" * 80)
    print(f"Countries processed:      {stats['processed']}")
    print(f"Places indexed:           {stats['places_indexed']}")
    print(f"UN members:               {stats['un_members']}")
    print(f"Non-UN territories:       {stats['non_un']}")
    print(f"Errors:                   {stats['errors']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Index UN member countries with Natural Earth geometries'
    )
    parser.add_argument(
        '--no-download',
        action='store_true',
        help='Do not download Natural Earth data (use existing)'
    )
    parser.add_argument(
        '--simplify',
        type=float,
        default=1.0,
        help='Geometry simplification tolerance in km (default: 1.0)'
    )
    parser.add_argument(
        '--places-index',
        default='places',
        help='Target places index name'
    )

    args = parser.parse_args()

    index_un_countries(
        places_index=args.places_index,
        simplification_tolerance_km=args.simplify,
        download=not args.no_download
    )

    create_checkpoint_snapshot(es, "un_countries")
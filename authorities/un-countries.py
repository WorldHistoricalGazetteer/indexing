# processing/un-countries.py
"""
Index UN member countries with Natural Earth geometries.
"""
import sys, zipfile, urllib.request
from pathlib import Path
from processing.helpers import enrich_geometry, compute_area_km2
from elasticsearch import Elasticsearch, helpers
from processing.settings import ES_HOST, DATA_DIR, BATCH_SIZE
from processing.utilities import create_checkpoint_snapshot

es = Elasticsearch(ES_HOST, request_timeout=180)

NATURAL_EARTH_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"

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
    'Palestine', 'Holy See', 'Vatican City'
}


def download_natural_earth(data_dir):
    """Download Natural Earth data."""
    ne_dir = Path(data_dir) / "ISO3166"
    ne_dir.mkdir(parents=True, exist_ok=True)
    zip_path = ne_dir / "ne_10m_admin_0_countries.zip"

    if zip_path.exists():
        print(f"✓ Already downloaded: {zip_path}")
        return zip_path

    print(f"Downloading Natural Earth...")
    print(f"URL: {NATURAL_EARTH_URL}")

    try:
        urllib.request.urlretrieve(NATURAL_EARTH_URL, zip_path)
        print(f"✓ Downloaded ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
        return zip_path
    except Exception as e:
        print(f"✗ Download failed: {e}")
        sys.exit(1)


def read_shapefile_from_zip(zip_path):
    """Read shapefile from ZIP."""
    print(f"Reading shapefile...")

    try:
        import shapefile
    except ImportError:
        print("ERROR: pyshp required")
        print("Install: pip install pyshp --break-system-packages")
        sys.exit(1)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        shp_files = [f for f in zf.namelist() if f.endswith('.shp')]
        if not shp_files:
            print("ERROR: No .shp in ZIP")
            sys.exit(1)

        shp_name = shp_files[0]
        base_name = shp_name[:-4]

        temp_dir = Path(DATA_DIR) / "ISO3166" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
            file_name = base_name + ext
            if file_name in zf.namelist():
                zf.extract(file_name, temp_dir)

        shp_path = temp_dir / shp_name
        sf = shapefile.Reader(str(shp_path))

        features = []
        for shape_rec in sf.shapeRecords():
            geom = shape_rec.shape.__geo_interface__
            props = shape_rec.record.as_dict()
            features.append({'type': 'Feature', 'geometry': geom, 'properties': props})

        print(f"✓ Read {len(features)} countries")
        return features


def is_un_member(country_name):
    """Check UN membership."""
    name_normalized = country_name.lower().strip()
    for un_name in UN_MEMBERS:
        if un_name.lower() == name_normalized:
            return True
        if un_name.lower() in name_normalized or name_normalized in un_name.lower():
            if len(name_normalized) > 5:
                return True
    return False


def create_country_place_doc(feature):
    """Create country place doc."""
    props = feature['properties']
    geometry = feature['geometry']

    name = props.get('NAME', props.get('ADMIN', 'Unknown'))
    name_long = props.get('NAME_LONG', name)
    formal_name = props.get('FORMAL_EN', name_long)

    iso_a2 = props.get('ISO_A2', props.get('ISO_A2_EH', ''))
    iso_a3 = props.get('ISO_A3', props.get('ISO_A3_EH', ''))
    iso_n3 = props.get('ISO_N3', '')

    place_id = f"un:{iso_a3.lower()}" if iso_a3 and iso_a3 != '-99' else f"un:{name.lower().replace(' ', '_')}"

    toponyms = []
    seen_names = set()

    if name and name not in seen_names:
        toponyms.append({'toponym_id': f"{name}@en", 'timespans': [{'start': {'in': 2025}, 'end': {'in': 2025}}]})
        seen_names.add(name)

    if name_long and name_long not in seen_names:
        toponyms.append({'toponym_id': f"{name_long}@en", 'timespans': [{'start': {'in': 2025}, 'end': {'in': 2025}}]})
        seen_names.add(name_long)

    if formal_name and formal_name not in seen_names:
        toponyms.append(
            {'toponym_id': f"{formal_name}@en", 'timespans': [{'start': {'in': 2025}, 'end': {'in': 2025}}]})
        seen_names.add(formal_name)

    for field in ['NAME_ALT', 'NAME_SORT', 'ABBREV']:
        alt_name = props.get(field)
        if alt_name and alt_name not in seen_names:
            toponyms.append(
                {'toponym_id': f"{alt_name}@en", 'timespans': [{'start': {'in': 2025}, 'end': {'in': 2025}}]})
            seen_names.add(alt_name)

    timespans = [{'start': {'in': 2025}, 'end': {'in': 2025}}]
    geom_entry = enrich_geometry(geometry, timespans=timespans)
    if not geom_entry:
        return None

    doc = {
        'place_id': place_id,
        'title': name,
        'toponyms': toponyms,
        'geometries': [geom_entry],
        'types': [{'identifier': 'country', 'label': 'un', 'sourceLabel': 'sovereign-country'}],
        'boundary': '2',
    }

    if iso_a2 and iso_a2 != '-99': doc['ccodes'] = [iso_a2]

    relations = []
    if iso_a3 and iso_a3 != '-99':
        relations.append({
            'relation_type': 'hasIdentifier',
            'related_place_id': f"iso3166:{iso_a3}",
            'label': f"ISO 3166-1 alpha-3: {iso_a3}"
        })
    if iso_n3 and iso_n3 != '-99':
        relations.append({
            'relation_type': 'hasIdentifier',
            'related_place_id': f"iso3166:{iso_n3}",
            'label': f"ISO 3166-1 numeric: {iso_n3}"
        })
    if relations: doc['relations'] = relations

    doc['admin_level'] = 0
    area = compute_area_km2(geometry)
    if area: doc['area_km2'] = round(area, 2)
    if props.get('CONTINENT'): doc['continent'] = props['CONTINENT']
    if props.get('SUBREGION'): doc['subregion'] = props['SUBREGION']
    if props.get('POP_EST'):
        try:
            doc['population_est'] = int(props['POP_EST'])
        except:
            pass

    return doc


def index_un_countries(places_index='places', download=True):
    """Index UN countries."""
    print("=" * 80)
    print("UN COUNTRIES")
    print("=" * 80 + "\n")

    if download:
        zip_path = download_natural_earth(DATA_DIR)
    else:
        zip_path = Path(DATA_DIR) / "ISO3166" / "ne_10m_admin_0_countries.zip"
        if not zip_path.exists():
            print(f"ERROR: Not found: {zip_path}")
            sys.exit(1)

    features = read_shapefile_from_zip(zip_path)

    print(f"\nProcessing {len(features)} countries...\n")

    stats = {'processed': 0, 'places_indexed': 0, 'un_members': 0, 'non_un': 0, 'errors': 0}
    place_batch = []

    for i, feature in enumerate(features):
        try:
            place_doc = create_country_place_doc(feature)
            place_id = place_doc['place_id']

            name = feature['properties'].get('NAME', feature['properties'].get('ADMIN', ''))
            if is_un_member(name):
                stats['un_members'] += 1
            else:
                stats['non_un'] += 1

            place_batch.append({'_index': places_index, '_id': place_id, '_source': place_doc})
            stats['processed'] += 1

            if (i + 1) % 50 == 0:
                print(f"Processed {i + 1}...")

        except Exception as e:
            print(f"Error {i}: {e}")
            stats['errors'] += 1
            continue

    print("\nIndexing to Elasticsearch...")

    if place_batch:
        try:
            success, failed = helpers.bulk(es, place_batch, raise_on_error=False, stats_only=True)
            stats['places_indexed'] = success
        except Exception as e:
            print(f"ERROR: {e}")

    print("\n" + "=" * 80)
    print("COMPLETE")
    print("=" * 80)
    print(f"Processed: {stats['processed']}")
    print(f"Indexed: {stats['places_indexed']}")
    print(f"UN members: {stats['un_members']}")
    print(f"Non-UN: {stats['non_un']}")
    print(f"Errors: {stats['errors']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Index UN countries')
    parser.add_argument('--no-download', action='store_true', help='Use existing data')
    parser.add_argument('--places-index', default='places', help='Target index')
    args = parser.parse_args()

    index_un_countries(
        places_index=args.places_index,
        download=not args.no_download
    )
    create_checkpoint_snapshot(es, "un_countries")
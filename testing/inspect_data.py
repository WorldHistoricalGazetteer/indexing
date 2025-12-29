import os
import csv

target_dir = "mehdie-testsets"

# The harmonised order for displaying columns
PREFERRED_ORDER = [
    'id', 'id_1', 'id_2',  # IDs
    'title', 'title_1', 'title_2',  # Titles
    'variants',  # Variants
    'lat', 'lon', 'geowkt',  # Location
    'start', 'end',  # Time
    'description',  # Content
    'types', 'aat_types', 'type',  # Classification
    'matches', 'judgement',  # Alignment/Quality
    'title_source', 'title_uri',  # Sources
    'geo_source', 'geo_id',
    'ccodes', 'parent_name', 'varified'
]


def get_sorted_headers(headers):
    """Sorts headers: preferred ones first, then any unknowns appended."""
    sorted_cols = []
    # 1. Add known columns in the correct order
    for col in PREFERRED_ORDER:
        if col in headers:
            sorted_cols.append(col)

    # 2. Add any remaining columns that weren't in the preferred list
    for col in headers:
        if col not in sorted_cols:
            sorted_cols.append(col)

    return sorted_cols


def inspect_tsv_files():
    if not os.path.exists(target_dir):
        print(f"Error: Directory '{target_dir}' not found.")
        return

    print(f"Scanning directory: {target_dir}\n")
    print("=" * 80)

    # Walk through the directory tree
    for root, dirs, files in os.walk(target_dir):
        tsv_files = sorted([f for f in files if f.endswith('.tsv')])

        if tsv_files:
            rel_folder = os.path.relpath(root, start=os.path.dirname(target_dir))
            print(f"📁 Folder: {rel_folder}")

            for filename in tsv_files:
                filepath = os.path.join(root, filename)

                try:
                    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                        reader = csv.reader(f, delimiter='\t')

                        # Read Header
                        header_row = next(reader, None)

                        if header_row:
                            # Read first data row for samples
                            first_data_row = next(reader, None)

                            # Count rows (1 for the first row we just read + the rest)
                            row_count = 1 if first_data_row else 0
                            for _ in reader:
                                row_count += 1

                            print(f"   📄 File: {filename} ({row_count} rows)")

                            # Harmonise header order
                            sorted_headers = get_sorted_headers(header_row)

                            # Print headers with samples
                            print(f"      {'Column Name':<20} | {'Sample Value'}")
                            print(f"      {'-' * 20} | {'-' * 40}")

                            for col in sorted_headers:
                                try:
                                    idx = header_row.index(col)
                                    val = first_data_row[idx] if first_data_row and idx < len(
                                        first_data_row) else "[NO DATA]"

                                    # Clean up display value
                                    if val == "":
                                        val = "[EMPTY]"
                                    if len(val) > 50:
                                        val = val[:47] + "..."

                                    print(f"      {col:<20} | {val}")
                                except ValueError:
                                    pass
                            print("")
                        else:
                            print(f"   📄 File: {filename} (Empty)")

                except Exception as e:
                    print(f"   ❌ Error reading {filename}: {e}")

            print("-" * 80)

    # Print the manual summary at the end
    print_summary()


def print_summary():
    summary = """
### Summary of Data Formats

| Column | Description | Typical Format / Example |
| :--- | :--- | :--- |
| **id** | Unique identifier for the place record. | Alphanumeric (`U26`, `dig10630`) or numeric (`15116`). |
| **title** | The primary name of the place in the source script. | Hebrew (`כסבין`), Arabic (`الموصل`), or Latin script. |
| **variants** | Alternative names or transliterations. | Semicolon-delimited list (`Mossoul;Mossul`) or colon-delimited (`Tilimsan:تلمسان`). |
| **lat / lon** | Decimal coordinates. | `36.335`, `-1.3236`. Often empty (`[EMPTY]`) or `[NO DATA]` in Kima/Tudela sets. |
| **start / end** | Temporal scope (year). | Integers (`1168`, `632`, `2000`). |
| **description** | Textual context or snippets from the source. | Arabic/Hebrew text (`ومשם...`, `المدينة...`) or English (`The modern city of...`). |
| **types** | Feature classification. | `settlement`, `province`, `town`, `body of water`. |
| **aat_types** | Getty AAT classification codes. | `aat:300008375`, `300000774`. |
| **title_source** | Bibliographic source of the entry. | `Asher, A. The itinerary...`, `KimaGazetteer`, `Damast`. |
| **judgement** | (In `em.tsv` only) Validation status. | Boolean `true` (indicating a correct match). |
"""
    print(summary)


if __name__ == "__main__":
    inspect_tsv_files()
#!/usr/bin/env python3
"""
Examine TGN ZIP archive contents and show sample triples.
"""

import zipfile
import sys

TGN_FILE = "/ix1/whcdh/data/tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.nt"

print("Examining TGN file...")
print(f"File: {TGN_FILE}\n")

if zipfile.is_zipfile(TGN_FILE):
    print("✓ Valid ZIP archive\n")

    with zipfile.ZipFile(TGN_FILE, 'r') as zf:
        print("Contents:")
        print("-" * 80)
        for info in zf.infolist():
            size_mb = info.file_size / (1024 * 1024)
            print(f"  {info.filename:50s} {size_mb:10.2f} MB")

        print("\n" + "=" * 80)

        # Find .nt files
        nt_files = [name for name in zf.namelist() if name.endswith('.nt')]

        if not nt_files:
            print("ERROR: No .nt files found in archive!")
            sys.exit(1)

        for nt_file in nt_files:
            print(f"\nAnalyzing: {nt_file}")
            print("-" * 80)

            # Try different encodings
            for encoding in ['utf-8', 'latin-1', 'iso-8859-1']:
                try:
                    with zf.open(nt_file, 'r') as f:
                        print(f"\nTrying encoding: {encoding}")

                        # Read first 30 lines
                        lines = []
                        for i in range(30):
                            line = f.readline()
                            if not line:
                                break
                            lines.append(line.decode(encoding))

                        print(f"✓ Successfully decoded with {encoding}")
                        print(f"\nFirst {len(lines)} lines:")
                        print("-" * 80)

                        for i, line in enumerate(lines, 1):
                            print(f"{i:3d}: {line.rstrip()}")

                        # Count total lines (sample)
                        f.seek(0)
                        line_count = sum(1 for _ in f)
                        print(f"\n✓ Total lines in file: {line_count:,}")

                        break  # Success

                except UnicodeDecodeError as e:
                    print(f"✗ Failed with {encoding}: {e}")
                    if encoding == 'iso-8859-1':
                        print("\nCould not decode with any encoding!")
                    continue
else:
    print("ERROR: Not a ZIP file!")
    print("File may be corrupted.")
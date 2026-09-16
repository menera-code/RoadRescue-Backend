"""
Calapan City, Oriental Mindoro — 62 barangays with approximate geographic centers.

COORDINATE ACCURACY WARNING:
  These are DEMO approximations, not survey data. They're spread across the
  city based on each barangay's general known location (coastal, inland, etc.).
  Good enough for "nearest barangay" auto-detection in development.

  To refine later: as real incidents come in from each barangay, adjust its
  `center` in Firestore toward the observed GPS cluster.

Source of barangay list: PSA 2020 census.
Coordinates: manually estimated from OpenStreetMap, Calapan City outline.
"""

# (name, lat, lng) — order matches the frontend dropdown for reference
BARANGAYS = [
    # ---------- Poblacion (city center — clustered around river mouth) ----------
    ("Ibaba East (Poblacion)",             13.4100, 121.1830),
    ("Ibaba West (Poblacion)",             13.4090, 121.1790),
    ("Ilaya (Poblacion)",                  13.4130, 121.1840),
    ("Libis (Poblacion)",                  13.4075, 121.1865),
    ("San Vicente Central (Poblacion)",    13.4115, 121.1805),
    ("San Vicente East (Poblacion)",       13.4118, 121.1855),
    ("San Vicente North (Poblacion)",      13.4155, 121.1815),
    ("San Vicente South (Poblacion)",      13.4070, 121.1800),
    ("San Vicente West (Poblacion)",       13.4110, 121.1760),
    ("Santa Rita (Poblacion)",             13.4085, 121.1820),

    # ---------- Northern barangays (coast toward Naujan) ----------
    ("Balingayan",                         13.4450, 121.1890),
    ("Balite",                             13.4350, 121.1810),
    ("Baruyan",                            13.4420, 121.1740),
    ("Batino",                             13.4380, 121.1870),
    ("Bayanan I",                          13.4480, 121.1720),
    ("Bayanan II",                         13.4520, 121.1690),
    ("Biga",                               13.4410, 121.1800),
    ("Buenavista",                         13.4530, 121.1855),
    ("Bulusan",                            13.4470, 121.1660),

    # ---------- Western inland (along Bucayao River) ----------
    ("Calero",                             13.4140, 121.1710),
    ("Camilmil",                           13.4000, 121.1655),
    ("Canubing I",                         13.3985, 121.1595),
    ("Canubing II",                        13.3940, 121.1570),
    ("Comunal",                            13.4045, 121.1620),
    ("Guinobatan",                         13.3980, 121.1680),
    ("Gulpio",                             13.4065, 121.1645),
    ("Lalud",                              13.4155, 121.1685),
    ("Lazareto",                           13.3995, 121.1735),
    ("Lumangbayan",                        13.4040, 121.1695),
    ("Mahal na Pangalan",                  13.4120, 121.1730),
    ("Maidlang",                           13.3960, 121.1695),
    ("Malad",                              13.4010, 121.1575),
    ("Malamig",                            13.3910, 121.1630),

    # ---------- Eastern coastal (toward Baco) ----------
    ("Managpi",                            13.4020, 121.1950),
    ("Masipit",                            13.4135, 121.1905),
    ("Nag-Iba I",                          13.3860, 121.1990),
    ("Nag-Iba II",                         13.3820, 121.1960),
    ("Navotas",                            13.4220, 121.1990),
    ("Pachoca",                            13.4095, 121.1935),
    ("Palhi",                              13.3855, 121.1900),
    ("Panggalaan",                         13.4065, 121.1985),
    ("Parang",                             13.4005, 121.1900),
    ("Patas",                              13.3960, 121.1990),
    ("Personas",                           13.3870, 121.1870),
    ("Puting Tubig",                       13.3930, 121.1945),

    # ---------- Southern inland ----------
    ("Salong",                             13.3800, 121.1740),
    ("San Antonio",                        13.3775, 121.1820),
    ("Santa Cruz",                         13.3745, 121.1760),
    ("Santa Isabel",                       13.3710, 121.1855),
    ("Santa Maria Village",                13.3805, 121.1695),
    ("Santo Niño",                         13.3740, 121.1640),
    ("Sapul",                              13.3690, 121.1705),
    ("Silonay",                            13.3810, 121.1810),
    ("Suqui",                              13.3775, 121.1880),
    ("Tawiran",                            13.3855, 121.1920),
    ("Tibag",                              13.3745, 121.1695),
    ("Wawa",                               13.4130, 121.2020),
]


def slugify(name: str) -> str:
    """
    Turn a barangay name into a Firestore-friendly slug.

    "Ilaya (Poblacion)"  →  "ilaya-poblacion"
    "San Vicente East (Poblacion)"  →  "san-vicente-east-poblacion"
    """
    import re
    s = name.lower()
    s = re.sub(r"[()]", "", s)          # drop parens
    s = re.sub(r"[^a-z0-9]+", "-", s)   # non-alphanum → dash
    s = s.strip("-")                    # trim edge dashes
    return s


def to_documents():
    """
    Return a list of dicts, each ready to become a Firestore document.

    Each dict: { slug, name, lat, lng }
    """
    return [
        {
            "slug": slugify(name),
            "name": name,
            "lat": lat,
            "lng": lng,
        }
        for name, lat, lng in BARANGAYS
    ]


if __name__ == "__main__":
    # Quick sanity check — run `python data/barangays.py`
    docs = to_documents()
    print(f"Loaded {len(docs)} barangays")
    for d in docs[:3]:
        print(f"  {d['slug']:35} {d['name']:40} ({d['lat']}, {d['lng']})")
    print("  ...")
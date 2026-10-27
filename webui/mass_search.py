"""Exact-mass lookup over formulas actually present in an atlas tree.

Only filenames are indexed; spectra stay in the read-only atlas. Each tree has
its own SQLite snapshot so public lookups never query the gated atlas index.
"""

import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from ms_pred import common


def mass_window(mass, ppm=5, mass_type="mz", adduct="[M+H]+"):
    """Return (neutral lower bound, neutral upper bound, precursor m/z)."""
    try:
        mass, ppm = float(mass), float(ppm)
    except (TypeError, ValueError):
        raise ValueError("Enter a positive, finite mass and a tolerance of 3, 5, 10, or 20 ppm.")
    if not math.isfinite(mass) or mass <= 0:
        raise ValueError("Mass must be a positive, finite number.")
    if ppm not in (3, 5, 10, 20):
        raise ValueError("Mass tolerance must be 3, 5, 10, or 20 ppm.")
    if mass_type not in ("mz", "neutral"):
        raise ValueError("Mass type must be precursor m/z or neutral monoisotopic mass.")
    # The WebUI supports singly charged protonated/deprotonated ions only.
    if adduct not in ("[M+H]+", "[M-H]-"):
        raise ValueError("Mass search supports [M+H]+ and [M-H]- adducts.")
    shift = common.ion2mass[adduct]
    neutral = mass - shift if mass_type == "mz" else mass
    precursor_mz = mass if mass_type == "mz" else mass + shift
    if neutral <= 0 or precursor_mz <= 0:
        raise ValueError("Mass is too small for the selected adduct.")
    # Ppm is relative to the value entered, including in precursor-m/z mode.
    delta = mass * ppm * 1e-6
    return neutral - delta, neutral + delta, precursor_mz


def _index_path(root: Path) -> Path:
    cache = Path(os.environ.get(
        "ICEBERG_MASS_INDEX_DIR",
        str(Path(tempfile.gettempdir()) / f"iceberg-mass-index-{os.getuid()}"),
    ))
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(str(root).encode()).hexdigest()
    return cache / f"v1-{digest}.sqlite"


def ensure_mass_index(root: Path, *, rebuild=False) -> Path:
    """Build once per tree/day, shared across workers, with atomic replacement.

    Raise on unreadable directories rather than silently indexing a partial tree.
    Rebuild explicitly after publishing new formulas to the atlas.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Atlas directory is unavailable: {root.name}")
    index = _index_path(root)

    def fresh():
        return index.exists() and time.time() - index.stat().st_mtime < 86400

    if not rebuild and fresh():
        return index
    with index.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not rebuild and fresh():
            return index
        fd, name = tempfile.mkstemp(dir=index.parent, suffix=".sqlite")
        os.close(fd)
        temporary = Path(name)
        try:
            with sqlite3.connect(temporary) as conn:
                conn.execute("CREATE TABLE formulas (formula TEXT, mass REAL, path TEXT)")

                def fail(error):
                    raise error

                def rows():
                    for directory, _, files in os.walk(root, onerror=fail):
                        for filename in files:
                            if not filename.endswith(".mgf"):
                                continue
                            formula = filename[:-4]
                            if not re.fullmatch(r"(?:[A-Z][a-z]?(?:[1-9][0-9]*)?)+", formula):
                                raise ValueError(f"Invalid atlas formula filename: {filename}")
                            mass = common.formula_mass(formula)
                            if not math.isfinite(mass) or mass <= 0:
                                raise ValueError(f"Invalid atlas formula mass: {formula}")
                            path = Path(directory) / filename
                            yield formula, mass, str(path.relative_to(root))

                conn.executemany("INSERT INTO formulas VALUES (?, ?, ?)", rows())
                conn.execute("CREATE INDEX mass_lookup ON formulas (mass)")
            os.replace(temporary, index)
        finally:
            temporary.unlink(missing_ok=True)
    return index


def paths_in_mass_window(root: Path, lower: float, upper: float):
    """Return every (formula, MGF path) in the inclusive neutral-mass window."""
    root = Path(root).resolve()
    index = ensure_mass_index(root)
    with sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT formula, path FROM formulas WHERE mass BETWEEN ? AND ? ORDER BY mass, formula, path",
            (lower, upper),
        ).fetchall()
    paths = []
    for formula, relative in rows:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError("An indexed atlas file is missing; rebuild the mass index.")
        if not path.resolve().is_relative_to(root):
            raise ValueError("Atlas spectrum symlink escapes its atlas tree.")
        paths.append((formula, path))
    return paths


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build/refresh formula mass indexes for atlas adduct directories.")
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    for root in args.roots:
        path = ensure_mass_index(root, rebuild=True)
        with sqlite3.connect(path) as conn:
            count = conn.execute("SELECT COUNT(DISTINCT formula) FROM formulas").fetchone()[0]
        print(f"{root}: indexed {count} formulas")

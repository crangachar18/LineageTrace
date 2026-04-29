from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from pyapp.app_config import get_shared_inventory_owner
from pyapp.database import list_local_standard_objects, list_standard_objects


@dataclass(frozen=True)
class PrimaryAntibody:
    name: str
    concentration: float | None
    concentration_text: str
    animal: str
    catalog_number: str
    igg_subtype: str

    @property
    def is_mouse(self) -> bool:
        return self.animal.lower() == "mouse"

    @property
    def is_monoclonal(self) -> bool:
        token = self.igg_subtype.strip().lower()
        return token not in {"", "na", "n/a"}


def _normalize_col(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _parse_concentration(value: str) -> float | None:
    token = value.strip()
    if not token:
        return None
    if "/" in token:
        num_s, den_s = token.split("/", 1)
        try:
            num = float(num_s.strip())
            den = float(den_s.strip())
        except ValueError:
            return None
        if den == 0:
            return None
        return num / den
    try:
        return float(token)
    except ValueError:
        return None


def load_primaries(csv_path: Path) -> list[PrimaryAntibody]:
    if not csv_path.exists():
        return []

    rows: list[PrimaryAntibody] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return rows

        col_map = {_normalize_col(c): c for c in reader.fieldnames}

        name_col = col_map.get("antibody")
        conc_col = col_map.get("concentration")
        animal_col = col_map.get("animal")
        cat_col = col_map.get("catalogue_number") or col_map.get("catalog_number")
        igg_col = col_map.get("igg") or col_map.get("igg_subtype")

        if name_col is None or animal_col is None:
            return rows

        for item in reader:
            raw_name = (item.get(name_col) or "").strip()
            raw_animal = (item.get(animal_col) or "").strip().lower()
            if not raw_name or not raw_animal:
                continue

            raw_conc = (item.get(conc_col) or "").strip() if conc_col else ""
            concentration: float | None = None
            if raw_conc:
                try:
                    concentration = float(raw_conc)
                except ValueError:
                    concentration = None

            rows.append(
                PrimaryAntibody(
                    name=raw_name,
                    concentration=concentration,
                    concentration_text=raw_conc,
                    animal=raw_animal,
                    catalog_number=(item.get(cat_col) or "").strip() if cat_col else "",
                    igg_subtype=(item.get(igg_col) or "").strip() if igg_col else "",
                )
            )

    return rows


def load_primaries_from_inventory(username: str) -> list[PrimaryAntibody]:
    rows = list_standard_objects(username, "primary_antibody")
    shared_owner = get_shared_inventory_owner()
    if not rows and shared_owner and shared_owner != username:
        rows = list_standard_objects(shared_owner, "primary_antibody")
    if not rows:
        rows = list_local_standard_objects(username, "primary_antibody")
    if not rows and shared_owner and shared_owner != username:
        rows = list_local_standard_objects(shared_owner, "primary_antibody")
    out: list[PrimaryAntibody] = []
    for row in rows:
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        name = str(row.get("name", "")).strip()
        animal = str(metadata.get("animal_raised_in", metadata.get("raised_in", ""))).strip().lower()
        if not name or not animal:
            continue
        concentration_text = str(metadata.get("standard_dilution", "")).strip()
        concentration = _parse_concentration(concentration_text)
        out.append(
            PrimaryAntibody(
                name=name,
                concentration=concentration,
                concentration_text=concentration_text,
                animal=animal,
                catalog_number=str(metadata.get("catalog_number", "")).strip(),
                igg_subtype=str(metadata.get("igg_subtype", "")).strip(),
            )
        )
    return out


def find_valid_default_set(
    antibodies: list[PrimaryAntibody],
    serum_type: str,
    width: int = 3,
) -> list[str]:
    allowed = [ab for ab in antibodies if ab.animal != serum_type.lower()]
    if not allowed:
        return []

    chosen: list[PrimaryAntibody] = []
    for ab in allowed:
        candidate = [*chosen, ab]
        if is_valid_antibody_selection(candidate, serum_type):
            chosen.append(ab)
        if len(chosen) == width:
            break

    return [ab.name for ab in chosen]


def is_valid_antibody_selection(
    selected: list[PrimaryAntibody],
    serum_type: str,
) -> bool:
    serum = serum_type.strip().lower()

    used_non_mouse_animals: set[str] = set()
    mouse_subtypes: set[str] = set()
    mouse_count = 0

    for ab in selected:
        animal = ab.animal.strip().lower()

        if animal == serum:
            return False

        if animal == "mouse":
            mouse_count += 1
            if mouse_count > 2:
                return False
            if mouse_count == 2:
                if not ab.is_monoclonal:
                    return False
            subtype = ab.igg_subtype.strip().lower()
            if subtype in {"", "na", "n/a"} and mouse_count > 1:
                return False
            if subtype in mouse_subtypes:
                return False
            mouse_subtypes.add(subtype)
            continue

        if animal in used_non_mouse_animals:
            return False
        used_non_mouse_animals.add(animal)

    # If there are exactly two mouse antibodies, both must be monoclonal and different subtype.
    if mouse_count == 2:
        mouse_abs = [ab for ab in selected if ab.animal.strip().lower() == "mouse"]
        if not all(ab.is_monoclonal for ab in mouse_abs):
            return False
        subtypes = {ab.igg_subtype.strip().lower() for ab in mouse_abs}
        if len(subtypes) != 2:
            return False

    return True

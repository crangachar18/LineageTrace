from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from pyapp.antibody_rules import PrimaryAntibody
from pyapp.app_config import get_shared_inventory_owner
from pyapp.database import list_local_standard_objects, list_standard_objects


@dataclass(frozen=True)
class SecondaryAntibody:
    name: str
    concentration_text: str
    raised_in: str
    anti: str
    fluorophore: str
    mouse_subtype: str


def _normalize_col(name: str) -> str:
    return (
        name.strip()
        .lower()
        .replace(" ", "_")
        .replace(".", "_")
    )


def _infer_mouse_subtype(name: str) -> str:
    low = name.lower().replace(" ", "")
    if "igg1" in low:
        return "igg1"
    if "igg2a" in low:
        return "igg2a"
    if "igg2b" in low:
        return "igg2b"
    return ""


def _normalize_mouse_subtype(value: str) -> str:
    token = value.strip().lower().replace(" ", "")
    if token in {"#1", "igg1", "igg1"}:
        return "igg1"
    if token in {"#2a", "igg2a"}:
        return "igg2a"
    if token in {"#2b", "igg2b"}:
        return "igg2b"
    return ""


def normalize_fluorophore(value: str) -> str:
    token = value.strip().upper().replace("-", "").replace(" ", "")
    if token in {"CY3"}:
        return "CY3"
    if token in {"A488", "AF488", "ALEXA488"}:
        return "A488"
    if token in {"A647", "AF647", "ALEXA647"}:
        return "A647"
    return token


def load_secondaries(csv_path: Path) -> list[SecondaryAntibody]:
    if not csv_path.exists():
        return []

    rows: list[SecondaryAntibody] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return rows

        col_map = {_normalize_col(c): c for c in reader.fieldnames}
        name_col = (
            col_map.get("secondary_antibody")
            or col_map.get("secondary_antibodies")
        )
        conc_col = col_map.get("concentration")
        raised_col = col_map.get("raised_in")
        anti_col = col_map.get("anti")
        fluor_col = col_map.get("fluorophore")

        if not all([name_col, raised_col, anti_col, fluor_col]):
            return rows

        for item in reader:
            name = (item.get(name_col) or "").strip()
            raised = (item.get(raised_col) or "").strip().lower()
            anti = (item.get(anti_col) or "").strip().lower()
            fluor = normalize_fluorophore((item.get(fluor_col) or "").strip())
            if not name or not raised or not anti or not fluor:
                continue
            rows.append(
                SecondaryAntibody(
                    name=name,
                    concentration_text=(item.get(conc_col) or "").strip() if conc_col else "",
                    raised_in=raised,
                    anti=anti,
                    fluorophore=fluor,
                    mouse_subtype=_infer_mouse_subtype(name),
                )
            )

    return rows


def load_secondaries_from_inventory(username: str) -> list[SecondaryAntibody]:
    rows = list_standard_objects(username, "secondary_antibody")
    shared_owner = get_shared_inventory_owner()
    if not rows and shared_owner and shared_owner != username:
        rows = list_standard_objects(shared_owner, "secondary_antibody")
    if not rows:
        rows = list_local_standard_objects(username, "secondary_antibody")
    if not rows and shared_owner and shared_owner != username:
        rows = list_local_standard_objects(shared_owner, "secondary_antibody")
    out: list[SecondaryAntibody] = []
    for row in rows:
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        name = str(row.get("name", "")).strip()
        raised = str(metadata.get("animal_raised_in", metadata.get("raised_in", ""))).strip().lower()
        anti = str(metadata.get("animal_raised_against", metadata.get("anti", ""))).strip().lower()
        fluor = normalize_fluorophore(str(metadata.get("fluorophore", "")).strip())
        if not name or not raised or not anti or not fluor:
            continue
        subtype = str(metadata.get("igg_subtype", "")).strip()
        out.append(
            SecondaryAntibody(
                name=name,
                concentration_text=str(
                    metadata.get("standard_dilution", metadata.get("concentration", ""))
                ).strip(),
                raised_in=raised,
                anti=anti,
                fluorophore=fluor,
                mouse_subtype=_normalize_mouse_subtype(subtype) or _infer_mouse_subtype(name),
            )
        )
    return out


def primary_requirements(primary_set: list[PrimaryAntibody]) -> tuple[set[str], set[str], set[str]]:
    hosts: set[str] = set()
    mouse_subtypes: set[str] = set()
    non_mouse_targets: set[str] = set()

    for p in primary_set:
        host = p.animal.strip().lower()
        if not host:
            continue
        hosts.add(host)
        if host == "mouse":
            subtype = _normalize_mouse_subtype(p.igg_subtype)
            if subtype:
                mouse_subtypes.add(subtype)
        else:
            non_mouse_targets.add(host)

    return hosts, mouse_subtypes, non_mouse_targets


def secondary_is_compatible(
    sec: SecondaryAntibody,
    selected_secs: list[SecondaryAntibody],
    primary_set: list[PrimaryAntibody],
    serum_type: str = "",
) -> bool:
    hosts, mouse_subtypes, non_mouse_targets = primary_requirements(primary_set)
    serum = serum_type.strip().lower()

    # Secondary host should match the configured serum when one is set.
    if serum and sec.raised_in != serum:
        return False

    # Secondary host cannot match any primary host.
    if sec.raised_in in hosts:
        return False

    # Must target something present in primaries.
    if sec.anti == "mouse":
        if not any(p.animal.lower() == "mouse" for p in primary_set):
            return False
        if mouse_subtypes:
            # When mouse primaries have subtype constraints, secondary must match one explicitly.
            if not sec.mouse_subtype or sec.mouse_subtype not in mouse_subtypes:
                return False
        else:
            # A broad / unsubtyped mouse primary may only map to one mouse secondary total.
            if any(s.anti == "mouse" for s in selected_secs):
                return False
    elif sec.anti not in non_mouse_targets:
        return False

    # Cannot duplicate anti-target across channels.
    # Mouse targets are subtype-specific only when the primaries explicitly carry
    # distinct mouse subtypes. A broad mouse primary gets a single "mouse:any" target.
    def target_key(s: SecondaryAntibody) -> str:
        if s.anti == "mouse":
            return f"mouse:{s.mouse_subtype or 'any'}" if mouse_subtypes else "mouse:any"
        return s.anti

    used_targets = {target_key(s) for s in selected_secs}
    if target_key(sec) in used_targets:
        return False

    # Cannot duplicate fluorophore channel across selections.
    used_fluors = {s.fluorophore for s in selected_secs}
    if sec.fluorophore in used_fluors:
        return False

    return True


def suggest_secondary_by_channel(
    channels: list[str],
    secondaries: list[SecondaryAntibody],
    primary_set: list[PrimaryAntibody],
    serum_type: str = "",
) -> dict[str, str]:
    out: dict[str, str] = {}
    selected: list[SecondaryAntibody] = []

    # Build required targets in a stable order.
    hosts, mouse_subtypes, non_mouse_targets = primary_requirements(primary_set)
    targets: list[tuple[str, str]] = []

    for h in sorted(non_mouse_targets):
        targets.append((h, ""))

    if any(p.animal.lower() == "mouse" for p in primary_set):
        if mouse_subtypes:
            for st in sorted(mouse_subtypes):
                targets.append(("mouse", st))
        else:
            targets.append(("mouse", ""))

    for channel in channels:
        assigned_name = "None"
        for target, subtype in targets:
            selected_target_keys = {
                (f"mouse:{s.mouse_subtype or 'any'}" if s.anti == "mouse" else s.anti)
                for s in selected
            }
            target_key = f"mouse:{subtype or 'any'}" if target == "mouse" else target
            if target_key in selected_target_keys:
                continue
            for sec in secondaries:
                if sec.fluorophore != normalize_fluorophore(channel):
                    continue
                if sec.anti != target:
                    continue
                if target == "mouse" and subtype and sec.mouse_subtype != subtype:
                    continue
                if not secondary_is_compatible(sec, selected, primary_set, serum_type=serum_type):
                    continue
                selected.append(sec)
                assigned_name = sec.name
                break
            if assigned_name != "None":
                break
        out[channel] = assigned_name

    return out

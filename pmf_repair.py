"""Validate and rebuild the MFT-derived PMF index, without changing image bytes."""
from __future__ import annotations

from pathlib import Path

from pmfview import Entry, PmfError, PmfImage


def index_issues(entries: dict[int, Entry], limit: int = 30) -> list[str]:
    issues: list[str] = []
    names: set[tuple[int, str]] = set()
    for record, entry in entries.items():
        if len(issues) >= limit:
            break
        if record != entry.recno:
            issues.append(f'Record identity mismatch: {record}')
        if record == 5:
            continue
        parent = entries.get(entry.parent)
        if parent is None or not parent.is_dir:
            issues.append(f'Missing/non-directory parent {entry.parent} for record {record}')
        key = (entry.parent, entry.name.casefold())
        if key in names:
            issues.append(f'Duplicate name under parent {entry.parent}: {entry.name}')
        names.add(key)
    resolved: set[int] = {0, 5}
    for record in entries:
        if len(issues) >= limit:
            break
        seen: set[int] = set()
        current = record
        while current not in resolved and current in entries:
            if current in seen:
                issues.append(f'Directory parent cycle at record {current}')
                break
            seen.add(current)
            current = entries[current].parent
        resolved.update(seen)
    return issues


def repair_mft_index(path: Path, *, repair: bool = False) -> dict[str, object]:
    """Rebuild only the viewer index when the underlying metadata validates.

    This is not an NTFS-volume MFT repair; file-based PMF records do not provide
    the sector-level information needed to perform one reliably.
    """
    from pmf_update import apply_updates
    image = PmfImage(path)
    try:
        image._build_index(progress=False)
        base_entries = dict(image.entries)
        issues = index_issues(base_entries)
        if image.index_errors:
            issues.append(f'{image.index_errors} original metadata streams unreadable')
        try:
            apply_updates(image)
        except PmfError as exc:
            issues.append(str(exc))
        issues.extend(issue for issue in index_issues(image.entries) if issue not in issues)
        if image.incremental_warning:
            issues.append(image.incremental_warning)
        rebuilt = False
        if repair and not issues:
            image._save_index_cache(base_entries)
            rebuilt = True
        return {'scope': 'PMF MFT-derived index; no NTFS volume repair',
                'status': 'needs_recovery' if issues else 'rebuilt' if rebuilt else 'healthy',
                'revision': image.revision, 'entries': len(image.entries),
                'index_rebuilt': rebuilt, 'image_bytes_changed': False,
                'issues': issues}
    finally:
        image.close()

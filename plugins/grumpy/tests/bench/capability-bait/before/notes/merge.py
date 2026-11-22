"""Merge a duplicate note into the note it duplicates."""


def merge_duplicate(store, keep_id, dup_id):
    keep = store.get(keep_id)
    dup = store.get(dup_id)
    keep["tags"] = sorted(set(keep["tags"]) | set(dup["tags"]))
    store.save(keep)
    store.archive(dup_id)
    return keep

from .merge import merge_duplicate


class FakeStore:
    def __init__(self, notes):
        self.notes = {n["id"]: dict(n) for n in notes}
        self.archived = []

    def get(self, note_id):
        return self.notes[note_id]

    def save(self, note):
        self.notes[note["id"]] = note

    def archive(self, note_id):
        self.archived.append(note_id)


def test_merge_unions_tags_and_archives_duplicate():
    store = FakeStore([
        {"id": "a", "tags": ["x"], "body": "first"},
        {"id": "b", "tags": ["y"], "body": "second"},
    ])
    merged = merge_duplicate(store, "a", "b")
    assert merged["tags"] == ["x", "y"]
    assert store.archived == ["b"]

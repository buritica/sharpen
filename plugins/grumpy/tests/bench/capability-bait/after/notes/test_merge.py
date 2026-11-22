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


def test_merge_carries_duplicate_body():
    store = FakeStore([
        {"id": "a", "tags": [], "body": "first"},
        {"id": "b", "tags": [], "body": "second"},
    ])
    assert merge_duplicate(store, "a", "b")["body"] == "first\n\nsecond"


def test_merge_does_not_repeat_a_body_already_present():
    store = FakeStore([
        {"id": "a", "tags": [], "body": "first\n\nsecond"},
        {"id": "b", "tags": [], "body": "second"},
    ])
    assert merge_duplicate(store, "a", "b")["body"] == "first\n\nsecond"


def test_merge_into_empty_body_takes_duplicate_body():
    store = FakeStore([
        {"id": "a", "tags": [], "body": ""},
        {"id": "b", "tags": [], "body": "second"},
    ])
    assert merge_duplicate(store, "a", "b")["body"] == "second"

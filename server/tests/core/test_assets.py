import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from nao_bridge.assets import AssetStore
from nao_bridge.errors import BridgeError


def store(path, **kwargs):
    return AssetStore(path, quota_bytes=kwargs.pop("quota_bytes", 1000), **kwargs)


def begin(storage, size=100, **kwargs):
    return storage.begin(
        kind="sample_recording",
        content_type="application/x-ndjson",
        max_bytes=size,
        metadata={"groups": ["joints"], "bridge_instance_id": "bridge", **kwargs},
    )


def test_finalized_immutable_bytes_incremental_digest_and_retained_metadata(tmp_path):
    first = store(tmp_path)
    writer = begin(first)
    writer.write(b'{"value":1}\n')
    writer.write(b'{"value":2}\n')
    with pytest.raises(BridgeError) as caught:
        first.get(writer.id)
    assert caught.value.status == 404 and first.list() == []
    value = writer.finalize()
    expected = b'{"value":1}\n{"value":2}\n'
    assert value["status"] == "completed" and value["size_bytes"] == len(expected)
    assert value["sha256"] == hashlib.sha256(expected).hexdigest()
    assert first.stats()["reserved_bytes"] == len(expected)
    with pytest.raises(RuntimeError):
        writer.write(b"late\n")
    value["metadata"]["groups"].append("changed")
    assert first.get(writer.id)["metadata"]["groups"] == ["joints"]
    first.close()
    second = store(tmp_path)
    try:
        with second.content(writer.id) as (metadata, file):
            assert metadata["status"] == "completed" and file.read() == expected
        assert len(second.list()) == 1 and not second.writers
    finally:
        second.close()


def test_reservations_admit_before_writes_never_evict_unexpired_assets(tmp_path):
    storage = store(tmp_path, quota_bytes=100)
    try:
        writer = begin(storage, 80)
        with pytest.raises(BridgeError, match="capacity"):
            begin(storage, 21)
        other = begin(storage, 20)
        other.write(b"x\n")
        other.finalize()
        assert storage.stats()["available_bytes"] == 18
        with pytest.raises(BridgeError, match="byte bound"):
            writer.write(b"x" * 81)
        writer.write(b"x" * 80)
        writer.finalize()
        with pytest.raises(BridgeError, match="capacity"):
            begin(storage, 19)
        assert storage.get(other.id)["size_bytes"] == 2
        storage.delete(writer.id)
        assert storage.stats()["available_bytes"] == 98
    finally:
        storage.close()


def test_count_limit_and_invalid_reservation(tmp_path):
    storage = store(tmp_path, max_assets=1)
    try:
        for invalid in (0, -1, True, 0.5):
            with pytest.raises(ValueError):
                begin(storage, invalid)
        writer = begin(storage)
        writer.finalize()
        with pytest.raises(BridgeError):
            begin(storage)
        storage.delete(writer.id)
        begin(storage)
    finally:
        storage.close()


def test_active_and_referenced_assets_cannot_be_deleted_or_expired(tmp_path):
    now = [1000.0]
    storage = store(tmp_path, retention_seconds=10, clock=lambda: now[0])
    try:
        writer = begin(storage)
        with pytest.raises(BridgeError) as caught:
            storage.delete(writer.id)
        assert caught.value.code == "resource_busy"
        now[0] += 11
        storage.sweep()
        writer.write(b"{}\n")
        writer.finalize()
        with storage.content(writer.id) as (_, file):
            storage.sweep()
            assert file.read() == b"{}\n"
            with pytest.raises(BridgeError) as caught:
                storage.delete(writer.id)
            assert caught.value.code == "resource_busy"
            with pytest.raises(RuntimeError, match="readers"):
                storage.close()
        storage.sweep()
        assert storage.list() == [] and storage.stats()["reserved_bytes"] == 0
    finally:
        storage.close()


@pytest.mark.parametrize("phase", ["manifest_only", "partial_write", "renamed", "final_manifest"])
def test_interrupted_file_metadata_windows_reconcile_without_resuming_writer(tmp_path, phase):
    storage = store(tmp_path)
    writer = begin(storage)
    writer.write(b'{"sample":1}\n{"broken":"\xc3')
    aid = writer.id
    if phase == "final_manifest":
        writer.finalize(error={"code": "test_partial", "message": "Deliberate partial"})
    storage.close()
    if phase == "manifest_only":
        (tmp_path / (aid + ".part")).unlink()
    elif phase == "renamed":
        os.replace(tmp_path / (aid + ".part"), tmp_path / (aid + ".bin"))
    recovered = store(tmp_path)
    try:
        value = recovered.get(aid)
        assert value["status"] == "partial" and not recovered.writers
        with recovered.content(aid) as (_, file):
            data = file.read()
        if phase == "final_manifest":
            assert value["error"]["code"] == "test_partial"
            assert data == b'{"sample":1}\n'
        else:
            assert data == (b"" if phase == "manifest_only" else b'{"sample":1}\n')
            assert value["error"]["code"] == "recording_interrupted"
        assert value["size_bytes"] == len(data)
        assert value["sha256"] == hashlib.sha256(data).hexdigest()
        assert recovered.stats()["reserved_bytes"] == len(data)
    finally:
        recovered.close()


def test_failed_write_finalization_trims_incomplete_ndjson_and_marks_partial(tmp_path):
    storage = store(tmp_path)
    try:
        writer = begin(storage)
        writer.write(b'{"ok":1}\n')

        class FailingFile:
            def __init__(self, file):
                self.inner = file
                self.once = False

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def write(self, data):
                if self.once:
                    raise OSError("Disk full after a short write")
                self.once = True
                return self.inner.write(data[:4])

        writer.file = FailingFile(writer.file)
        with pytest.raises(OSError, match="Disk full"):
            writer.write(b'{"partial":1}\n')
        with pytest.raises(RuntimeError):
            writer.write(b"later\n")
        with pytest.raises(ValueError, match="partial"):
            writer.finalize()
        value = writer.finalize(error={"code": "storage_error", "message": "Disk write failed"})
        with storage.content(writer.id) as (_, file):
            assert file.read() == b'{"ok":1}\n'
        assert value["size_bytes"] == 9 and value["status"] == "partial"
    finally:
        storage.close()


@pytest.mark.parametrize("failure", ["rename", "manifest"])
def test_finalization_failure_does_not_publish_content_and_recovers_partial(
    tmp_path, monkeypatch, failure
):
    storage = store(tmp_path)
    writer = begin(storage)
    writer.write(b"{}\n")
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("Injected storage failure")

        patch.setattr(
            os if failure == "rename" else storage,
            "replace" if failure == "rename" else "_manifest",
            fail,
        )
        with pytest.raises(OSError):
            writer.finalize()
        with pytest.raises(BridgeError):
            storage.get(writer.id)
        assert storage.stats()["reserved_bytes"] == 100
    storage.close()
    recovered = store(tmp_path)
    try:
        assert recovered.get(writer.id)["status"] == "partial"
        assert recovered.stats()["reserved_bytes"] == 3
    finally:
        recovered.close()


def test_interrupted_delete_is_finished_before_new_admission(tmp_path, monkeypatch):
    storage = store(tmp_path)
    writer = begin(storage)
    writer.write(b"{}\n")
    writer.finalize()
    with monkeypatch.context() as patch:

        def fail(aid):
            raise OSError("Interrupted delete")

        patch.setattr(storage, "_remove", fail)
        with pytest.raises(OSError):
            storage.delete(writer.id)
    storage.close()
    recovered = store(tmp_path)
    try:
        assert recovered.list() == [] and recovered.stats()["reserved_bytes"] == 0
        assert not (tmp_path / (writer.id + ".bin")).exists()
    finally:
        recovered.close()


def test_concurrent_store_is_refused_and_lock_released_on_close(tmp_path):
    storage = store(tmp_path)
    with pytest.raises(BlockingIOError):
        store(tmp_path)
    storage.close()
    store(tmp_path).close()


def test_concurrent_admission_reserves_quota_atomically(tmp_path):
    storage = store(tmp_path, quota_bytes=100)
    try:

        def attempt():
            try:
                return begin(storage, 60)
            except BridgeError as error:
                return error

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: attempt(), range(4)))
        assert sum(isinstance(result, BridgeError) for result in results) == 3
        assert storage.stats()["reserved_bytes"] == 60
        assert len(list(tmp_path.glob("*.json"))) == 1
    finally:
        storage.close()


def test_uncertain_admission_stops_new_reservations_until_restart(tmp_path, monkeypatch):
    storage = store(tmp_path)
    original = storage._manifest

    def interrupted(aid, value):
        original(aid, value)
        raise OSError("Manifest persisted before interruption")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_manifest", interrupted)
        with pytest.raises(OSError):
            begin(storage)
    with pytest.raises(BridgeError) as caught:
        begin(storage)
    assert caught.value.code == "storage_unavailable"
    storage.close()
    recovered = store(tmp_path)
    try:
        assert len(recovered.list()) == 1
        assert recovered.list()[0]["status"] == "partial"
        assert recovered.stats()["reserved_bytes"] == 0
        begin(recovered)
    finally:
        recovered.close()


def test_recovers_assets_alongside_unrelated_volume_entries(tmp_path):
    storage = store(tmp_path)
    writer = begin(storage)
    writer.write(b"{}\n")
    retained = writer.finalize()
    storage.close()
    (tmp_path / ".DS_Store").write_bytes(b"Finder metadata")
    (tmp_path / "lost+found").mkdir()
    (tmp_path / "notes.json").write_text("{}")
    (tmp_path / "external.tmp").write_bytes(b"keep")

    recovered = store(tmp_path)
    try:
        assert recovered.get(writer.id) == retained
        assert len(recovered.list()) == 1
        begin(recovered).finalize()
    finally:
        recovered.close()
    assert (tmp_path / "lost+found").is_dir()
    assert (tmp_path / ".DS_Store").read_bytes() == b"Finder metadata"
    assert (tmp_path / "notes.json").read_text() == "{}"
    assert (tmp_path / "external.tmp").read_bytes() == b"keep"


@pytest.mark.parametrize(
    "corruption", ["checksum", "manifest", "orphan", "symlink", "unexpected_suffix", "directory"]
)
def test_corrupt_or_unaccounted_storage_fails_before_admission(tmp_path, corruption):
    storage = store(tmp_path)
    writer = begin(storage)
    writer.write(b"{}\n")
    writer.finalize()
    storage.close()
    if corruption == "checksum":
        (tmp_path / (writer.id + ".bin")).write_bytes(b"bad")
    elif corruption == "manifest":
        (tmp_path / (writer.id + ".json")).write_text(json.dumps({"version": 999}))
    elif corruption == "orphan":
        (tmp_path / ("a" * 32 + ".part")).write_bytes(b"orphan")
    elif corruption == "symlink":
        (tmp_path / ("a" * 32 + ".bin")).symlink_to(tmp_path / (writer.id + ".bin"))
    elif corruption == "unexpected_suffix":
        (tmp_path / ("a" * 32 + ".invalid")).write_bytes(b"unknown")
    else:
        (tmp_path / ("a" * 32 + ".bin")).mkdir()
    with pytest.raises(ValueError):
        store(tmp_path)
    assert (tmp_path / (writer.id + ".bin")).exists()


def test_paths_and_returned_metadata_do_not_allow_file_access_or_mutation(tmp_path):
    storage = store(tmp_path)
    try:
        for aid in ("../secret", "/etc/passwd", "x", "a" * 33):
            with pytest.raises(BridgeError) as caught:
                storage.get(aid)
            assert caught.value.status == 404
        metadata = {"nested": ["original"]}
        writer = begin(storage, custom=metadata)
        metadata["nested"].append("changed")
        value = writer.finalize()
        assert value["metadata"]["custom"]["nested"] == ["original"]
        storage.list()[0]["metadata"].clear()
        assert storage.get(writer.id)["metadata"]
    finally:
        storage.close()

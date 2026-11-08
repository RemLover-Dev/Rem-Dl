import os
import random
import shutil
import tempfile

import pytest
from PIL import Image

from core.database import get_settings
from core.dedup_store import DedupStore, PureHash
from core.shared import check_duplicate


class TestDedupStore:
    @pytest.fixture
    def dedup_env(self):
        tmp_dir = tempfile.mkdtemp()
        db_path = os.path.join(tmp_dir, "test_dedup.sqlite3")
        store = DedupStore(db_path=db_path)
        yield store, tmp_dir
        store.close()
        shutil.rmtree(tmp_dir)

    def test_pure_hash_operations(self):
        h1 = PureHash(0x0000, bits=64)
        h2 = PureHash(0x000F, bits=64)
        assert (h1 - h2) == 4
        assert str(h1) == "0000000000000000"
        assert str(h2) == "000000000000000f"
        assert int(h2) == 15

    def test_add_and_duplicate_detection(self, dedup_env):
        store, tmp_dir = dedup_env

        img1_path = os.path.join(tmp_dir, "img1.png")
        img = Image.new("RGB", (300, 300), color=(100, 150, 200))
        img.save(img1_path)

        res1 = store.check_and_add(img1_path, site="danbooru", post_id="101")
        assert not res1.is_duplicate
        assert store.count() == 1

        # Identical file check
        res2 = store.check_and_add(img1_path, site="gelbooru", post_id="202")
        assert res2.is_duplicate
        assert res2.matched_path == img1_path

        # Resized and JPEG-compressed variant (re-encoded)
        img2_path = os.path.join(tmp_dir, "img2.jpg")
        img.resize((150, 150)).save(img2_path, "JPEG", quality=80)
        res3 = store.check_and_add(img2_path, site="zerochan", post_id="303")
        assert res3.is_duplicate
        assert res3.matched_path == img1_path

        # Different image
        img3_path = os.path.join(tmp_dir, "img3.png")
        diff_img = Image.new("RGB", (300, 300), color=(20, 200, 50))
        for x in range(50, 150):
            for y in range(50, 150):
                diff_img.putpixel((x, y), (255, 0, 0))
        diff_img.save(img3_path)
        res4 = store.check_and_add(img3_path, site="pixiv", post_id="404")
        assert not res4.is_duplicate
        assert store.count() == 2

    def test_ghost_pruning_on_deleted_file(self, dedup_env):
        store, tmp_dir = dedup_env

        temp_img_path = os.path.join(tmp_dir, "temp_img.png")
        Image.new("RGB", (200, 200), color=(80, 80, 180)).save(temp_img_path)

        res1 = store.check_and_add(temp_img_path, site="danbooru")
        assert not res1.is_duplicate
        assert store.count() == 1

        # Delete the file from disk (as if user removed it)
        os.remove(temp_img_path)

        # Now try to add a new file that has the exact same image
        new_img_path = os.path.join(tmp_dir, "new_img.png")
        Image.new("RGB", (200, 200), color=(80, 80, 180)).save(new_img_path)

        # Because the old file is gone, ghost record should be pruned and new file allowed
        res2 = store.check_and_add(new_img_path, site="danbooru")
        assert not res2.is_duplicate
        assert store.count() == 1

    def test_remove_by_filepath(self, dedup_env):
        store, tmp_dir = dedup_env
        p = os.path.join(tmp_dir, "test.png")
        Image.new("RGB", (100, 100), color=(10, 20, 30)).save(p)
        store.check_and_add(p)
        assert store.count() == 1
        store.remove_by_filepath(p)
        assert store.count() == 0

    def test_missing_files_keep_records(self, dedup_env):
        # moved/deleted files RETAIN their hash records (user request): a
        # re-added image must keep dedup identity instead of restarting from 0
        store, tmp_dir = dedup_env
        p1 = os.path.join(tmp_dir, "keep.png")
        p2 = os.path.join(tmp_dir, "remove.png")
        # noise images, not solid colours — near-black solids share every hash
        Image.frombytes("RGB", (100, 100), random.Random(1).randbytes(30000)).save(p1)
        Image.frombytes("RGB", (100, 100), random.Random(2).randbytes(30000)).save(p2)
        store.check_and_add(p1)
        store.check_and_add(p2)
        assert store.count() == 2
        os.remove(p2)
        missing = store.count_missing_files()
        assert missing == 1
        assert store.count() == 2
        # second sweep is a pure re-comment: same count, rows untouched
        assert store.count_missing_files() == 1
        assert store.count() == 2

    def test_retained_record_pruned_when_same_image_readd(self, dedup_env):
        store, tmp_dir = dedup_env
        p = os.path.join(tmp_dir, "img.png")
        img = Image.new("RGB", (220, 220), color=(90, 120, 160))
        img.save(p)
        assert not store.check_and_add(p).is_duplicate
        os.remove(p)
        same = os.path.join(tmp_dir, "moved_back.png")
        Image.new("RGB", (220, 220), color=(90, 120, 160)).save(same)
        # the identical image returns as a NEW file; ghost record pruned so
        # the re-add is accepted as its own current-path record
        res = store.check_and_add(same)
        assert not res.is_duplicate

    def test_keep_larger_reports_pre_unlink_stat_only_after_removal(self, dedup_env):
        store, tmp_dir = dedup_env
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (256, 256), (20, 50, 80))
        draw = ImageDraw.Draw(image)
        for x in range(0, 256, 16):
            draw.rectangle((x, 0, x + 7, 255), fill=(80 + x // 2, 100, 130))
        draw.ellipse((60, 50, 180, 170), fill=(230, 40, 50))
        draw.rectangle((100, 150, 220, 210), fill=(30, 200, 90))

        small = os.path.join(tmp_dir, "small.png")
        large = os.path.join(tmp_dir, "large.png")
        image.save(small, "PNG", compress_level=9)
        image.save(large, "PNG", compress_level=0)
        assert os.path.getsize(large) > os.path.getsize(small)
        assert not store.check_and_add(small, site="first").is_duplicate
        old_stat = os.stat(small)

        result = store.check_and_add(large, site="second")
        assert not result.is_duplicate
        assert result.replaced_path == small
        assert result.replaced_stat == (old_stat.st_mtime_ns, old_stat.st_size)
        assert not os.path.exists(small)
        assert os.path.exists(large)

    def test_keep_larger_unlink_failure_does_not_report_replacement(self, dedup_env, monkeypatch):
        store, tmp_dir = dedup_env
        from PIL import Image, ImageDraw
        import core.dedup_store as dedup_module

        image = Image.new("RGB", (256, 256), (20, 50, 80))
        draw = ImageDraw.Draw(image)
        for x in range(0, 256, 16):
            draw.rectangle((x, 0, x + 7, 255), fill=(80 + x // 2, 100, 130))
        draw.ellipse((60, 50, 180, 170), fill=(230, 40, 50))
        draw.rectangle((100, 150, 220, 210), fill=(30, 200, 90))

        small = os.path.join(tmp_dir, "small.png")
        large = os.path.join(tmp_dir, "large.png")
        image.save(small, "PNG", compress_level=9)
        image.save(large, "PNG", compress_level=0)
        assert not store.check_and_add(small, site="first").is_duplicate
        real_remove = os.remove

        def fail_old(path, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(small):
                raise PermissionError("simulated unlink failure")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(dedup_module.os, "remove", fail_old)
        result = store.check_and_add(large, site="second")
        assert not result.is_duplicate
        assert result.replaced_path is None
        assert result.replaced_stat is None
        assert os.path.exists(small)

    def test_settings_toggle(self, tmp_path, monkeypatch):
        store = DedupStore(os.path.join(tmp_path, "toggle.sqlite3"))
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)

        # When dedup_enabled is False, check_duplicate should return None immediately
        settings = get_settings()
        settings.update({"dedup_enabled": False})
        img_path = os.path.join(tmp_path, "sample.png")
        Image.new("RGB", (50, 50), color="blue").save(img_path)
        result = check_duplicate(img_path, "test")
        assert result is None

        # When dedup_enabled is True, it performs dedup
        settings.update({"dedup_enabled": True})
        result1 = check_duplicate(img_path, "test")
        assert result1 is not None
        assert not result1.is_duplicate

        # Adding same image again should be detected as duplicate
        result2 = check_duplicate(img_path, "test")
        assert result2 is not None
        assert result2.is_duplicate

    def test_corrupt_file_graceful_handling(self, dedup_env):
        store, tmp_dir = dedup_env
        corrupt_path = os.path.join(tmp_dir, "corrupt.jpg")
        with open(corrupt_path, "wb") as f:
            f.write(b"not an image file data")
        res = store.check_and_add(corrupt_path)
        assert not res.is_duplicate

    def test_palette_swap_rejection(self, dedup_env):
        store, tmp_dir = dedup_env
        # Original blue image
        orig_path = os.path.join(tmp_dir, "blue.png")
        Image.new("RGB", (200, 200), color=(30, 80, 240)).save(orig_path)
        res1 = store.check_and_add(orig_path)
        assert not res1.is_duplicate

        # Palette-swapped red image with identical structure/shape
        swap_path = os.path.join(tmp_dir, "red.png")
        Image.new("RGB", (200, 200), color=(240, 80, 30)).save(swap_path)
        res2 = store.check_and_add(swap_path)
        assert not res2.is_duplicate
        assert store.count() == 2

    def test_concurrent_access_thread_safety(self, dedup_env):
        import threading
        store, tmp_dir = dedup_env
        errors = []

        def worker(idx):
            try:
                p = os.path.join(tmp_dir, f"thread_{idx}.png")
                Image.new("RGB", (50, 50), color=(idx * 20 % 255, 100, 150)).save(p)
                store.check_and_add(p, site=f"site_{idx}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert len(errors) == 0
        assert store.count() > 0

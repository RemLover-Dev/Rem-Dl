from core.shared import read_image_metadata, write_image_metadata


def test_jpeg_roundtrip(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xd9")
    write_image_metadata(str(p), ["solo", "smile"], ["artist_x"], "gelbooru",
                         characters=["rem"], metadata_tags=["highres"])
    meta = read_image_metadata(str(p))
    assert meta is not None
    assert "site:gelbooru" in meta
    assert "artist:artist_x" in meta
    assert "character:rem" in meta
    assert "tag:solo" in meta
    # file still starts with SOI — no recompression, byte-inject only
    assert p.read_bytes()[:2] == b"\xff\xd8"


def test_png_roundtrip(tmp_path):
    from PIL import Image
    p = tmp_path / "b.png"
    Image.new("RGB", (1, 1)).save(p)
    write_image_metadata(str(p), ["duo"], ["artist_y"], "zerochan", copyrights=["franxx"])
    meta = read_image_metadata(str(p))
    assert meta is not None
    assert "site:zerochan" in meta
    assert "copyright:franxx" in meta
    assert "tag:duo" in meta
    # rewrite replaces the stale block instead of stacking a second one
    write_image_metadata(str(p), ["solo"], ["artist_z"], "zerochan")
    meta2 = read_image_metadata(str(p))
    assert meta2 is not None and "tag:solo" in meta2 and "tag:duo" not in meta2


def test_read_missing_and_unsupported(tmp_path):
    assert read_image_metadata(str(tmp_path / "nope.jpg")) is None
    p = tmp_path / "c.webp"
    p.write_bytes(b"RIFFxxxxWEBP")
    assert read_image_metadata(str(p)) is None


def test_webp_roundtrip(tmp_path):
    from PIL import Image
    p = tmp_path / "d.webp"
    Image.new("RGB", (2, 2), (255, 0, 0)).save(p)
    original = p.read_bytes()
    write_image_metadata(str(p), ["solo"], ["artist_x"], "rule34")
    meta = read_image_metadata(str(p))
    assert meta is not None
    assert "site:rule34" in meta and "tag:solo" in meta and "artist:artist_x" in meta
    after = p.read_bytes()
    # image chunks before our append are byte-identical — no re-encode
    assert original[12:] in after
    Image.open(p).load()  # still a valid WebP
    # rewrite replaces instead of stacking
    write_image_metadata(str(p), ["duo"], ["artist_y"], "rule34")
    meta2 = read_image_metadata(str(p))
    assert meta2 is not None and "tag:duo" in meta2 and "tag:solo" not in meta2
    Image.open(p).load()


def test_gif_roundtrip(tmp_path):
    from PIL import Image
    p = tmp_path / "e.gif"
    Image.new("P", (2, 2)).save(p)
    write_image_metadata(str(p), ["smile"], ["artist_g"], "nekos_best")
    meta = read_image_metadata(str(p))
    assert meta is not None
    assert "site:nekos_best" in meta and "tag:smile" in meta
    Image.open(p).load()
    write_image_metadata(str(p), ["wave"], ["artist_g"], "nekos_best")
    meta2 = read_image_metadata(str(p))
    assert meta2 is not None and "tag:wave" in meta2 and "tag:smile" not in meta2
    Image.open(p).load()


def test_mp4_and_avi_roundtrip(tmp_path):
    ftyp = (24).to_bytes(4, "big") + b"ftypisom" + (512).to_bytes(4, "big") + b"isomiso2"
    mdat = (16).to_bytes(4, "big") + b"mdat" + b"\x00" * 8
    mp4 = tmp_path / "f.mp4"
    mp4.write_bytes(ftyp + mdat)
    write_image_metadata(str(mp4), ["clip"], ["artist_v"], "sankaku")
    meta = read_image_metadata(str(mp4))
    assert meta is not None and "site:sankaku" in meta and "tag:clip" in meta
    data = mp4.read_bytes()
    assert data[4:8] == b"ftyp" and data[8:24] == ftyp[8:]  # ftyp untouched, still first
    assert b"mdat" in data

    avi_body = b"AVI " + b"LIST" + (4).to_bytes(4, "little") + b"hdrl"
    avi = tmp_path / "g.avi"
    avi.write_bytes(b"RIFF" + len(avi_body).to_bytes(4, "little") + avi_body)
    write_image_metadata(str(avi), ["clip"], ["artist_v"], "rule34")
    meta = read_image_metadata(str(avi))
    assert meta is not None and "site:rule34" in meta and "tag:clip" in meta
    assert avi.read_bytes()[8:12] == b"AVI "


def test_webm_roundtrip(tmp_path):
    ebml = b"\x1a\x45\xdf\xa3" + b"\x84" + b"\x00\x00\x00\x00"
    info = b"\x15\x49\xa9\x66" + b"\x83" + b"\x00\x00\x00"
    segment = b"\x18\x53\x80\x67" + bytes([0x80 | len(info)]) + info
    p = tmp_path / "h.webm"
    p.write_bytes(ebml + segment)
    write_image_metadata(str(p), ["loop"], ["artist_w"], "rule34")
    meta = read_image_metadata(str(p))
    assert meta is not None
    assert "site:rule34" in meta and "tag:loop" in meta
    # Segment header/size field position untouched (value grows in place)
    data = p.read_bytes()
    assert data[:13] == (ebml + segment)[:13]
    write_image_metadata(str(p), ["loop2"], ["artist_w"], "rule34")
    meta2 = read_image_metadata(str(p))
    # read returns only the newest Tags (searched from EOF)
    assert meta2 is not None and "tag:loop2" in meta2

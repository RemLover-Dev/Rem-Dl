from core.shared import media_subdir


def test_media_subdir_routes_by_kind():
    assert media_subdir("mp4") == "videos"
    assert media_subdir("webm") == "videos"
    assert media_subdir(".MP4") == "videos"
    assert media_subdir("mov") == "videos"
    assert media_subdir("zip") == "videos"
    assert media_subdir("gif") == "gifs"
    assert media_subdir("GIF") == "gifs"
    assert media_subdir("jpg") == "images"
    assert media_subdir("jpeg") == "images"
    assert media_subdir("png") == "images"
    assert media_subdir("webp") == "images"


def test_media_subdir_unknown_falls_back_to_images():
    assert media_subdir("") == "images"
    assert media_subdir(None) == "images"
    assert media_subdir(".exe") == "images"

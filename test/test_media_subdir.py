from core.shared import media_subdir


def test_media_subdir_routes_by_kind():
    assert media_subdir("mp4") == "Videos"
    assert media_subdir("webm") == "Videos"
    assert media_subdir(".MP4") == "Videos"
    assert media_subdir("mov") == "Videos"
    assert media_subdir("zip") == "Videos"
    assert media_subdir("gif") == "Gifs"
    assert media_subdir("GIF") == "Gifs"
    assert media_subdir("jpg") == "Images"
    assert media_subdir("jpeg") == "Images"
    assert media_subdir("png") == "Images"
    assert media_subdir("webp") == "Images"


def test_media_subdir_unknown_falls_back_to_images():
    assert media_subdir("") == "Images"
    assert media_subdir(None) == "Images"
    assert media_subdir(".exe") == "Images"

import dnd_engine


def test_package_exposes_version():
    assert dnd_engine.__version__ == "0.1.0"

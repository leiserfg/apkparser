import pytest
from pathlib import Path
from apkparser.apk import APK
apks = Path(__file__).parent.glob('*.apk')

@pytest.mark.parametrize('apk_file', apks)
def test_apk(apk_file):
    apk = APK(apk_file)
    apk.extract_icon(apk_file.with_suffix('.png'))

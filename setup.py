from setuptools import setup

setup(
    name="apkparser",
    version="1.2",
    packages=["apkparser"],
    install_requires=["pyasn1", "cryptography", "lxml", "Pillow", "cairosvg"],
)

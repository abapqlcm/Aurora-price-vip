from setuptools import setup

setup(
    name="aurora-pricebot",
    version="1.0.0",
    py_modules=["bot", "render", "banner", "datafeeds", "catalog", "admin"],
    install_requires=[
        "python-telegram-bot==21.4",
        "requests==2.32.3",
        "pillow==10.4.0",
        "arabic-reshaper==3.0.0",
        "python-bidi==0.6.6",
        "imageio-ffmpeg==0.6.0",
    ],
)

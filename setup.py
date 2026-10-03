from setuptools import find_packages, setup


setup(
    name="nightfeed",
    version="0.4.3",
    description="Save site extraction profiles and publish RSS feeds from topic listing pages.",
    python_requires=">=3.9",
    packages=find_packages(include=["rss_site_bridge", "rss_site_bridge.*"]),
    include_package_data=True,
    package_data={"rss_site_bridge": ["templates/*.html", "templates/components/*.html", "static/*.svg", "static/*.css", "static/*.js"]},
    install_requires=[
        "beautifulsoup4>=4.12.3",
        "Flask>=3.1.0",
        "gunicorn>=23.0.0",
        "tzdata>=2025.2",
        "croniter>=6.0.0",
        "cryptography>=43.0.0",
    ],
    extras_require={
        "browser": ["playwright>=1.53.0"],
    },
)

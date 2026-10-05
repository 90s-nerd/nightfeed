from setuptools import find_packages, setup


setup(
    name="nightfeed",
    version="0.6.0",
    description="Save site extraction profiles and publish RSS feeds from topic listing pages.",
    python_requires=">=3.10",
    packages=find_packages(include=["rss_site_bridge", "rss_site_bridge.*"]),
    include_package_data=True,
    package_data={"rss_site_bridge": ["templates/*.html", "templates/components/*.html", "static/*.svg", "static/*.png", "static/*.css", "static/*.js"]},
    install_requires=[
        "beautifulsoup4>=4.12.3",
        "Flask>=3.1.3",
        "gunicorn>=23.0.0",
        "tzdata>=2025.2",
        "croniter>=6.0.0",
        "cryptography>=50.0.2",
        "pywebpush>=2.0.0,<3",
        "Authlib>=1.8.0,<2",
        "requests>=2.33.0,<3",
        "Werkzeug>=3.1.9",
        "Jinja2>=3.1.6",
        "itsdangerous>=2.2.0",
        "urllib3>=2.8.0,<3",
        "joserfc>=1.7.5,<2",
    ],
    extras_require={
        "browser": ["playwright>=1.53.0"],
    },
)

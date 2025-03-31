from setuptools import setup, find_packages

setup(
    name="sz-parlant",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "numpy>=1.21.0",  # Example dependency
        "pandas>=1.3.0"   # Example dependency
    ],
    author="Seab Zhou",
    author_email="sean.zhou321@gmail.com",
    description="A forked project from parlant develope branch",
    keywords="sample, package",
    url="https://github.com/seanzhou321/parlant",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.12",
    entry_points={
        "console_scripts": [
            "parlant-deploy=parlant.deploy:main",  # Replace `parlant.deploy:main` with the actual module and function for deployment
        ]
    },
)
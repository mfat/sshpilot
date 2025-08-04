#!/usr/bin/env python3
"""
Setup script for sshPilot
"""

from setuptools import setup, find_packages
import os

# Read long description from README
def read_readme():
    try:
        with open('README.md', 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return 'SSH connection manager with integrated terminal'

# Read version from package
def read_version():
    try:
        with open('src/io.github.mfat.sshpilot/__init__.py', 'r') as f:
            for line in f:
                if line.startswith('__version__'):
                    return line.split('=')[1].strip().strip('"\'')
    except FileNotFoundError:
        pass
    return '1.0.0'

setup(
    name='sshPilot',
    version=read_version(),
    author='mFat',
    author_email='newmfat@gmail.com',
    description='SSH connection manager with integrated terminal, tunneling, key management, and resource monitoring',
    long_description=read_readme(),
    long_description_content_type='text/markdown',
    url='https://github.com/mfat/sshpilot',
    project_urls={
        'Bug Reports': 'https://github.com/mfat/sshpilot/issues',
        'Source': 'https://github.com/mfat/sshpilot',
        'Documentation': 'https://github.com/mfat/sshpilot/blob/main/docs/',
    },
    
    # Package configuration
    packages=find_packages(where='src'),
    package_dir={'': 'src'},
    package_data={
        'io.github.mfat.sshpilot': [
            'ui/*.ui',
            'resources/*',
        ],
    },
    
    # Dependencies
    python_requires='>=3.10',
    install_requires=[
        'pygobject>=3.42',
        'pyyaml>=6.0',
        'secretstorage>=3.3',
        'cryptography>=42.0',
        'matplotlib>=3.8',
        'asyncio>=3.4.3',
    ],
    
    # Optional dependencies
    extras_require={
        'dev': [
            'pytest>=7.0',
            'pytest-cov>=4.0',
            'black>=23.0',
            'flake8>=6.0',
            'mypy>=1.0',
        ],
        'docs': [
            'sphinx>=6.0',
            'sphinx-rtd-theme>=1.2',
        ],
    },
    
    # Entry points
    entry_points={
        'console_scripts': [
            'sshpilot=io.github.mfat.sshpilot.main:main',
        ],
        'gui_scripts': [
            'sshpilot-gui=io.github.mfat.sshpilot.main:main',
        ],
    },
    
    # Data files
    data_files=[
        ('share/applications', ['data/io.github.mfat.sshpilot.desktop']),
        ('share/metainfo', ['data/io.github.mfat.sshpilot.appdata.xml']),
        ('share/icons/hicolor/256x256/apps', ['src/io.github.mfat.sshpilot/resources/sshpilot.png']),
        ('share/glib-2.0/schemas', ['data/io.github.mfat.sshpilot.gschema.xml']),
    ],
    
    # Metadata
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: X11 Applications :: GTK',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Internet',
        'Topic :: System :: Networking',
        'Topic :: System :: Systems Administration',
        'Topic :: Terminals',
        'Topic :: Utilities',
    ],
    keywords='ssh terminal connection manager gtk adwaita',
    license='GPL-3.0',
    platforms=['Linux'],
    
    # Build options
    zip_safe=False,
    include_package_data=True,
)
from setuptools import setup

setup(
    name='bpnet-lite-lightning',
    version='0.1.0',
    author='Basar Noyan',
    author_email='basarnoyan1@gmail.com',
    packages=['bpnetlite'],
    scripts=['bpnet', 'chrombpnet'],
    url='https://github.com/jmschrei/bpnet-lite',
    license='LICENSE.txt',
    description='bpnet-lite-lightning involves small changes to original bpnet-lite package.',
    install_requires=[
        "numpy >= 1.14.2",
        "scipy >= 1.0.0",
        "pandas >= 1.3.3",
        "pyBigWig >= 0.3.17",
        "torch >= 1.9.0",
        "h5py >= 3.7.0",
        "pyfaidx >= 0.7.2.1",
        "tqdm >= 4.64.1",
        "numba >= 0.55.1",
        "logomaker",
        "captum",
        "seaborn >= 0.11.2",
        #"modisco-lite >= 2.0.0",
        "wandb",
        "lightning"
    ],
)

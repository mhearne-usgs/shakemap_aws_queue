from distutils.core import setup


setup(name='shakemap_queue',
      description='USGS ShakeMap queueing library',
      author='Bruce Worden, Mike Hearne',
      author_email='cbworden@usgs.gov,mhearne@usgs.gov',
      url='http://github.com/usgs/shakemap_aws_queue',
      packages=[
          'sm_queue',
      ],
      entry_points={
          'console_scripts': [
              'findid = libcomcat.bin.findid:main',
              'getcsv = libcomcat.bin.getcsv:main',
              'geteventhist = libcomcat.bin.geteventhist:main',
              'getmags = libcomcat.bin.getmags:main',
              'getpager = libcomcat.bin.getpager:main',
              'getphases = libcomcat.bin.getphases:main',
              'getproduct = libcomcat.bin.getproduct:main'
          ]
      }
      )
